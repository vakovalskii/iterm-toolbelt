import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def service(t, monkeypatch):
    closed = asyncio.Event()
    connection = SimpleNamespace(websocket=SimpleNamespace(
        wait_closed=AsyncMock(side_effect=closed.wait)))
    servers = []
    listen = asyncio.start_server

    async def start_server(*args, **kwargs):
        server = await listen(*args, **kwargs)
        servers.append((server, server.sockets[0].getsockname()))
        return server

    monkeypatch.setitem(t.CFG, "port", 0)
    monkeypatch.setattr(t, "CONN", {"c": None})
    monkeypatch.setattr(t.asyncio, "start_server", start_server)
    return SimpleNamespace(t=t, connection=connection, closed=closed,
                           servers=servers, listen=listen)


async def assert_stopped(service):
    assert service.t.CONN.get("c") is None
    assert len(service.servers) == 1
    server, address = service.servers[0]
    assert not server.is_serving()
    assert not server.sockets
    # A new process must be able to use the same HTTP port immediately.
    async with await service.listen(lambda reader, writer: writer.close(), *address):
        pass


def test_disconnect_interrupts_initialization(service, monkeypatch):
    async def scenario():
        started = asyncio.Event()
        tasks = []

        async def get_app(connection):
            tasks.append(asyncio.current_task())
            started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(service.t.iterm2, "async_get_app", get_app, raising=False)
        main = asyncio.create_task(service.t.main(service.connection))
        await asyncio.wait_for(started.wait(), 1)
        assert service.t.CONN["c"] is service.connection
        service.closed.set()
        await asyncio.wait_for(main, 1)
        assert tasks[0].cancelled()
        await assert_stopped(service)
        assert asyncio.all_tasks() == {asyncio.current_task()}

    asyncio.run(scenario())


@pytest.mark.parametrize("shutdown", ["disconnect", "disconnect_cleanup_error", "cancel"])
def test_running_session_stops_background_tasks(service, monkeypatch, shutdown):
    async def scenario():
        t = service.t
        app = SimpleNamespace(current_terminal_window=None)
        monitor = AsyncMock()
        monitor.__aenter__.return_value = monitor
        if shutdown == "disconnect_cleanup_error":
            # FocusMonitor may fail to unsubscribe after the socket is closed.
            monitor.__aexit__.side_effect = ConnectionError("socket closed")
        focus_waiting = asyncio.Event()
        poll_waiting = asyncio.Event()
        autosave_waiting = asyncio.Event()
        tasks = {}
        updates = [SimpleNamespace(active_session_changed=SimpleNamespace(session_id="focused"),
                                   selected_tab_changed=None, window_changed=None)]

        async def next_update():
            if updates:
                return updates.pop()
            focus_waiting.set()
            await asyncio.Event().wait()

        async def sleep(delay):
            tasks["poll"] = asyncio.current_task()
            poll_waiting.set()
            await asyncio.Event().wait()

        async def autosave(app):
            tasks["autosave"] = asyncio.current_task()
            autosave_waiting.set()
            await asyncio.Event().wait()

        monitor.async_get_next_update.side_effect = next_update
        monkeypatch.setitem(t.CFG, "tabs", {})
        monkeypatch.setattr(t.iterm2, "async_get_app", AsyncMock(return_value=app), raising=False)
        monkeypatch.setattr(t.iterm2, "FocusMonitor", lambda connection: monitor, raising=False)
        monkeypatch.setattr(t.iterm2, "tool", SimpleNamespace(
            async_register_web_view_tool=AsyncMock()), raising=False)
        monkeypatch.setattr(t, "refresh_session", AsyncMock())
        monkeypatch.setattr(t, "ensure_toolbelt", AsyncMock())
        monkeypatch.setattr(t, "autosave_loop", autosave)
        monkeypatch.setattr(t.asyncio, "sleep", sleep)

        main = asyncio.create_task(t.main(service.connection))
        await asyncio.wait_for(asyncio.gather(focus_waiting.wait(), poll_waiting.wait(),
                                             autosave_waiting.wait()), 1)
        assert not main.done()
        assert t.CONN["c"] is service.connection
        t.refresh_session.assert_awaited_once_with(app, "focused")
        assert t.iterm2.tool.async_register_web_view_tool.await_count == len(t.TABS)
        assert service.servers[0][0].is_serving()

        if shutdown.startswith("disconnect"):
            service.closed.set()
            await asyncio.wait_for(main, 1)
        else:
            main.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(main, 1)

        assert all(task.cancelled() for task in tasks.values())
        monitor.__aexit__.assert_awaited_once()
        await assert_stopped(service)
        assert asyncio.all_tasks() == {asyncio.current_task()}

    asyncio.run(scenario())


def test_initialization_error_is_not_hidden(service, monkeypatch):
    async def scenario():
        monkeypatch.setattr(service.t.iterm2, "async_get_app",
                            AsyncMock(side_effect=RuntimeError("initialization failed")), raising=False)
        with pytest.raises(RuntimeError, match="initialization failed"):
            await asyncio.wait_for(service.t.main(service.connection), 1)
        await assert_stopped(service)
        assert asyncio.all_tasks() == {asyncio.current_task()}

    asyncio.run(scenario())


def test_disconnect_does_not_wait_for_pending_http_rpc(service, monkeypatch):
    async def scenario():
        initialized = asyncio.Event()
        requested = asyncio.Event()
        release_request = asyncio.Event()

        async def session(connection):
            initialized.set()
            await asyncio.Event().wait()

        async def open_agent(query):
            requested.set()
            await release_request.wait()
            return "ok"

        monkeypatch.setattr(service.t, "iterm_session", session)
        monkeypatch.setattr(service.t, "open_agent", open_agent)
        main = asyncio.create_task(service.t.main(service.connection))
        await asyncio.wait_for(initialized.wait(), 1)
        _, address = service.servers[0]
        reader, writer = await asyncio.open_connection(*address)
        try:
            writer.write(b"POST /sessions/open HTTP/1.1\r\nX-Toolbelt: 1\r\n\r\n")
            await writer.drain()
            await asyncio.wait_for(requested.wait(), 1)
            service.closed.set()
            await asyncio.wait_for(main, 1)
            await assert_stopped(service)
        finally:
            release_request.set()
            await asyncio.wait_for(reader.read(), 1)
            writer.close()
            await writer.wait_closed()

    asyncio.run(scenario())

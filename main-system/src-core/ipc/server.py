"""IPC server package — WebSocket-based command bridge for GPTBridge.

This module is the public entry point and re-export hub for the IPC server
subsystem.  The implementation has been subdivided into focused sub-modules:

  * ``server_tokens``    — session-token management, authorization, constants
  * ``server_process``   — port-owner detection and stale-process inspection
  * ``server_commands``  — command-result helpers, logging, task processing
  * ``server_handler``   — WebSocket connection handler, status push loop
  * ``server_lifecycle`` — ``run_server`` entry point, noise filter, HTTP helper

All public names are re-exported here so that ``from ipc.server import X``
continues to work unchanged.
"""

from __future__ import annotations

# Re-export everything that was previously defined at module level so that
# existing imports (``from ipc.server import run_server``, etc.) keep working.

from .server_tokens import (  # noqa: F401
    DEFAULT_IPC_PORT,
    IPC_PORT_ENV,
    IPC_SESSION_TOKEN_ENV,
    IPC_STATE_ROOT_ENV,
    SHUTDOWN_TOKEN_ENV,
    _IPC_SESSION_TOKEN,
    _IPC_TOKEN_LOCK_NAME,
    _IPC_TOKEN_LOCK_WAIT_SECONDS,
    _IPC_TOKEN_PATTERN,
    _IPC_TOKEN_STALE_LOCK_SECONDS,
    _WINDOWS_FILE_REPLACE_RETRY_INTERVAL_SECONDS,
    _WINDOWS_FILE_REPLACE_RETRY_SECONDS,
    _background_subprocess_kwargs,
    _break_stale_ipc_token_lock,
    _get_or_create_ipc_session_token,
    _harden_private_path,
    _ipc_port,
    _ipc_session_token_file,
    _ipc_state_root,
    _read_ipc_token,
    _repair_or_create_ipc_token,
    _replace_path_atomically,
    _shutdown_request_authorized,
    _shutdown_request_is_manual,
    _valid_ipc_session_token,
    _websocket_request_authorized,
    _workspace_instance_id,
    _write_ipc_token_atomically,
)
from .server_process import (  # noqa: F401
    _get_port_owner,
    _is_gptbridge_process,
    _kill_process,
    _query_process_commandline,
    _tasklist_image_name,
)
from .server_commands import (  # noqa: F401
    _toolbox_result_log_payload,
    _write_core_log_safely,
    command_result_ok,
    process_command_task,
    result_event_for_command,
)
from .server_handler import (  # noqa: F401
    MAX_CONNECTION_COMMAND_TASKS,
    _runtime_status_push_loop,
    handler,
)
from .server_lifecycle import (  # noqa: F401
    TRUSTED_WEBSOCKET_ORIGINS,
    _ExpectedProbeNoiseFilter,
    http_response,
    run_server,
)

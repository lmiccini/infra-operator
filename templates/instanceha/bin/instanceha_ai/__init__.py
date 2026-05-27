"""InstanceHA AI Layer — tool abstraction, safety/approval, LLM integration."""

import logging

from .tools import (
    ApprovalLevel,
    Tool,
    ToolRegistry,
    ToolResult,
    registry,
    tool,
)
from .safety import (
    ApprovalManager,
    AuditLogger,
    RateLimiter,
)

from . import read_tools       # noqa: F401
from . import write_tools      # noqa: F401
from . import diagnostic_tools # noqa: F401

_observer = None


def start_ai_services(connection, service, config_manager):
    """Start all AI services (observer, chat, MCP). Called from instanceha.py main()."""
    global _observer

    config = config_manager
    ai_section = {}
    if hasattr(config, 'get_config_value'):
        for key in ('enabled', 'backend', 'endpoint', 'model', 'api_key',
                     'model_path', 'mcp_enabled', 'chat_enabled', 'observer_enabled'):
            val = config.get_config_value(f"AI_{key.upper()}")
            if val is not None:
                ai_section[key] = val

    if ai_section.get('enabled', 'false').lower() != 'true':
        logging.info("AI layer disabled (ai.enabled != true)")
        return

    from .event_bus import get_event_bus
    bus = get_event_bus()

    approval_manager = ApprovalManager(
        auto_approve_level=ApprovalLevel.NONE,
        dry_run=False,
    )

    if ai_section.get('observer_enabled', 'true').lower() == 'true':
        from .observer import Observer
        _observer = Observer(bus)
        _observer.start()
        logging.info("AI observer started")

    llm_engine = None
    backend = ai_section.get('backend', '')
    if backend:
        from .engine import create_engine
        llm_engine = create_engine(ai_section)
        if llm_engine and llm_engine.is_available():
            logging.info("LLM engine started (backend=%s)", backend)
        else:
            logging.warning("LLM engine not available (backend=%s)", backend)
            llm_engine = None

    if ai_section.get('chat_enabled', 'true').lower() == 'true':
        from .chat_server import ChatServer
        chat = ChatServer(
            nova_connection=connection,
            service=service,
            approval_manager=approval_manager,
            llm_engine=llm_engine,
        )
        chat.start()
        logging.info("Chat server started")

    if ai_section.get('mcp_enabled', 'false').lower() == 'true':
        from .mcp_server import InstanceHAMCPServer
        mcp = InstanceHAMCPServer(
            nova_connection=connection,
            service=service,
            approval_manager=approval_manager,
        )
        thread = mcp.start()
        if thread:
            logging.info("MCP server started")

    logging.info("AI layer initialization complete")

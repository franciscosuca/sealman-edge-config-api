from unittest.mock import AsyncMock

import pytest

from extensions.registry import _build_detail


@pytest.mark.asyncio
async def test_build_detail_includes_action_descriptions():
    extension_repo = AsyncMock()
    extension_repo.get_extension_row.return_value = {
        "name": "widgets",
        "description": "Widget extension",
        "schema_version": 1,
        "enabled": False,
    }
    extension_repo.list_upstreams.return_value = []
    extension_repo.list_routes.return_value = []
    extension_repo.list_extension_action_specs.return_value = [
        {"name": "widgets.read", "description": "Read widgets"},
    ]

    detail = await _build_detail(extension_repo, "widgets")

    assert [action.model_dump() for action in detail.actions] == [
        {"name": "widgets.read", "description": "Read widgets"},
    ]
    extension_repo.list_extension_actions.assert_not_called()

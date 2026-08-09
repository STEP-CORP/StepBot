"""Проверка обновлений переживает переименование репозитория.

GitHub отвечает на старое имя редиректом 301; httpx по умолчанию за ним не идёт, из-за чего
у всех уже установленных ботов (в конфиге сохранено старое имя) кнопка «Обновление» писала
«Не удалось проверить GitHub».
"""

from __future__ import annotations

import httpx
import respx

from src.infrastructure.services.updater import check_for_update

OLD = "https://api.github.com/repos/OLD-ORG/OLD-NAME/commits/main"
NEW = "https://api.github.com/repositories/12345/commits/main"


@respx.mock
async def test_renamed_repo_still_resolves() -> None:
    respx.get(OLD).mock(return_value=httpx.Response(301, headers={"Location": NEW}))
    respx.get(NEW).mock(
        return_value=httpx.Response(
            200, json={"sha": "abcdef1234567890", "commit": {"message": "новое"}}
        )
    )
    info = await check_for_update("OLD-ORG/OLD-NAME", "main", "0000000")
    assert info.latest == "abcdef123456"
    assert info.available is True


@respx.mock
async def test_unreachable_github_is_not_fatal() -> None:
    respx.get(OLD).mock(side_effect=httpx.ConnectError("no network"))
    info = await check_for_update("OLD-ORG/OLD-NAME", "main", "0000000")
    assert info.latest == "" and info.available is False

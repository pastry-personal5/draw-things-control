"""``GET /v1/inputs``."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from draw_things_control.server.context import ServerContext
from draw_things_control.server.dependencies import Page, get_context, get_page, require_auth
from draw_things_control.server.pagination import next_cursor
from draw_things_control.server.serializers import input_image
from draw_things_control.services.input_listing import list_inputs

router = APIRouter(dependencies=[Depends(require_auth)])


@router.get("/v1/inputs")
def get_inputs(context: ServerContext = Depends(get_context), page: Page = Depends(get_page)) -> dict[str, object]:
    images = list_inputs(context.global_config.input_directory)[page.offset : page.offset + page.limit]
    return {"inputs": [input_image(image) for image in images], "cursor": next_cursor(page.offset, page.limit, len(images))}

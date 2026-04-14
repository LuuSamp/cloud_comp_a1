"""Simulation workflow placeholders (501); replace with real orchestration later."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

router = APIRouter(prefix="/sim", tags=["simulation"])

_PLACEHOLDER_DETAIL = (
    "Not implemented; use existing REST endpoints or implement workflow here."
)


class SimPlaceOrderIn(BaseModel):
    customer_id: int
    food_place_id: int


class SimOrderTransitionIn(BaseModel):
    order_status_id: int = Field(..., ge=1, le=6)
    detail: str | None = None


@router.post("/orders/place")
def sim_place_order(_body: SimPlaceOrderIn) -> JSONResponse:
    return JSONResponse(
        status_code=501,
        content={"detail": _PLACEHOLDER_DETAIL},
    )


@router.post("/orders/{order_id}/transition")
def sim_order_transition(order_id: int, _body: SimOrderTransitionIn) -> JSONResponse:
    return JSONResponse(
        status_code=501,
        content={"detail": _PLACEHOLDER_DETAIL, "order_id": order_id},
    )

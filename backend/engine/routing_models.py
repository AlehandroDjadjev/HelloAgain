from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class RouteDecision(BaseModel):
    route: Literal["direct_reasoning", "live_tool", "semi_agent"]
    reason: str = Field(min_length=1, max_length=240)
    tool_name: Optional[str] = Field(
        default=None,
        description="Required only for live_tool. Expected values: weather, time, search.",
    )
    location: Optional[str] = Field(
        default=None,
        description="Optional extracted location for time or weather requests.",
    )

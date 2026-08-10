from typing import Optional

from pydantic import BaseModel, Field


class NearestNodeResponse(BaseModel):
    node_id: int
    distance_m: float
    within_threshold: bool
    threshold_m: float = Field(
        ..., description="The threshold actually used for this request."
    )

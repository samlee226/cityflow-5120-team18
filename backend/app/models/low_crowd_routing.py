from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class CrowdStatus(str, Enum):
    """Whether this edge had any sensor with known crowd data in range.

    'unknown' means no sensor with a resolvable ratio was within the
    proximity radius -- it does NOT mean the area is calm, just that
    there's no data either way. Only 'known' edges ever contribute to
    sensor_coverage_ratio's numerator.
    """
    known = "known"
    unknown = "unknown"


class LowCrowdRouteStep(BaseModel):
    seq: int
    edge_id: Optional[int]
    node_id: int

    cost: float = Field(..., description="Weighted cost pgr_dijkstra actually used for this edge.")
    agg_cost: float = Field(..., description="Cumulative weighted cost up to this step.")
    base_distance_m: Optional[float] = Field(
        default=None, description="True unweighted distance of this edge in metres. None on the terminal step."
    )

    crowd_status: CrowdStatus
    crowd_ratio: Optional[float] = Field(
        default=None,
        description="Highest effective_crowd_ratio among sensors within the proximity radius. None when crowd_status is 'unknown'.",
    )

    geometry: Optional[dict] = Field(
        default=None,
        description="GeoJSON LineString for this edge, oriented in the direction of travel. None on the terminal step.",
    )


class LowCrowdRouteResponse(BaseModel):
    start_node: int
    end_node: int

    total_cost: float = Field(..., description="Sum of weighted costs along the chosen route.")
    total_distance_m: float = Field(..., description="True unweighted distance of the chosen route, in metres.")

    sensor_coverage_ratio: float = Field(
        ...,
        description="Fraction (0-1) of the route's distance that passed within reach of a sensor with known crowd data. Low coverage means the crowd-avoidance claim for this route is weakly supported by data, not that the route is uncrowded.",
    )
    crowd_aggregation_method: str = Field(
        default="max",
        description="How multiple sensors within range of the same edge are combined. 'max' = the highest nearby ratio wins (most cautious).",
    )
    proximity_radius_m: float = Field(
        ..., description="Distance in metres within which a sensor is considered to affect an edge."
    )

    steps: List[LowCrowdRouteStep]
    route_geometry: Optional[dict] = Field(
        default=None,
        description="GeoJSON LineString for the full path, merged from all step geometries in order.",
    )

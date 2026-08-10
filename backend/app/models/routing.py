from typing import List, Optional

from pydantic import BaseModel, Field


class RouteRequest(BaseModel):
    start_node: int = Field(..., description="Node ID in routing_edges_pgr's vertex network")
    end_node: int = Field(..., description="Destination node ID")


class RouteStep(BaseModel):
    seq: int
    edge_id: Optional[int]
    node_id: int
    cost: float
    agg_cost: float
    geometry: Optional[dict] = Field(
        default=None,
        description="GeoJSON LineString for this edge, oriented in the direction of travel. None on the final (terminal) step.",
    )


class RouteResponse(BaseModel):
    start_node: int
    end_node: int
    total_cost: float
    steps: List[RouteStep]
    route_geometry: Optional[dict] = Field(
        default=None,
        description="GeoJSON LineString for the full path, merged from all step geometries in order.",
    )

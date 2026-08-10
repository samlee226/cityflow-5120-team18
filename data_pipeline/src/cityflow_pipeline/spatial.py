"""Deterministic in-memory processing for the CityFlow pedestrian network.

Source GeoJSON and database-ready WKT remain in WGS84 (EPSG:4326). All
length, endpoint matching, and nearest-node calculations use the configured
projected CRS in metres (EPSG:32755 by default for Melbourne).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
import math
from types import MappingProxyType
from typing import Final, Literal
from uuid import NAMESPACE_URL, uuid5

import networkx as nx
import numpy as np
import pandas as pd
from pyproj import CRS, Transformer


NETWORK_SOURCE_COLUMNS: Final = (
    "Geo Point",
    "Geo Shape",
    "OBJECTID",
    "NeworkID",
)
ROUTING_NODE_COLUMNS: Final = (
    "node_id",
    "source_object_id",
    "source_network_id",
    "longitude",
    "latitude",
    "geometry_wkt",
    "component_id",
    "is_primary_component",
    "node_origin",
)
ROUTING_EDGE_COLUMNS: Final = (
    "edge_id",
    "source_object_id",
    "source_network_id",
    "source_node_id",
    "target_node_id",
    "length_m",
    "cost",
    "reverse_cost",
    "geometry_wkt",
    "component_id",
    "is_primary_component",
    "duplicate_geometry",
)
SENSOR_MAPPING_COLUMNS: Final = (
    "sensor_id",
    "node_id",
    "snap_distance_m",
    "within_snap_threshold",
    "network_component_id",
    "is_primary_component",
)
LANDMARK_MAPPING_COLUMNS: Final = (
    "landmark_id",
    "node_id",
    "snap_distance_m",
    "within_snap_threshold",
    "network_component_id",
    "is_primary_component",
)
EXCLUDED_SPATIAL_DATASETS: Final = (
    "pedestrian_counts_hourly",
    "pedestrian_counts_minutely",
)


class SpatialProcessingError(ValueError):
    """Raised when spatial input cannot satisfy the processing contract."""


@dataclass(frozen=True, slots=True)
class SpatialProcessingConfig:
    """Coordinate systems and configurable snapping tolerances."""

    geographic_crs: str = "EPSG:4326"
    projected_crs: str = "EPSG:32755"
    endpoint_tolerance_m: float = 0.25
    sensor_snap_threshold_m: float = 100.0
    landmark_snap_threshold_m: float = 250.0
    zero_length_tolerance_m: float = 0.001
    max_reported_issues: int = 20

    def __post_init__(self) -> None:
        for name in (
            "endpoint_tolerance_m",
            "sensor_snap_threshold_m",
            "landmark_snap_threshold_m",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a positive finite number")
        if (
            not math.isfinite(self.zero_length_tolerance_m)
            or self.zero_length_tolerance_m < 0
        ):
            raise ValueError(
                "zero_length_tolerance_m must be a non-negative finite number"
            )
        if isinstance(self.max_reported_issues, bool) or self.max_reported_issues < 0:
            raise ValueError("max_reported_issues must be a non-negative integer")
        try:
            geographic = CRS.from_user_input(self.geographic_crs)
            projected = CRS.from_user_input(self.projected_crs)
        except Exception as error:
            raise ValueError("configured CRS is invalid") from error
        if not geographic.is_geographic:
            raise ValueError("geographic_crs must be geographic")
        if not projected.is_projected:
            raise ValueError("projected_crs must be projected in metres")
        axis_units = {axis.unit_name.lower() for axis in projected.axis_info}
        if not axis_units or not all("metre" in unit or "meter" in unit for unit in axis_units):
            raise ValueError("projected_crs axes must use metres")


@dataclass(frozen=True, slots=True)
class SpatialIssue:
    """One reported source geometry problem retained for review."""

    code: str
    source_row: int
    source_object_id: object
    message: str


@dataclass(frozen=True, slots=True)
class SpatialProcessingReport:
    """Geometry, routing, and topology metrics for one processed network."""

    source_row_count: int
    geometry_type_counts: Mapping[str, int]
    valid_geometry_count: int
    invalid_geometry_count: int
    missing_geometry_count: int
    malformed_geometry_count: int
    unsupported_geometry_count: int
    invalid_coordinate_count: int
    point_geometry_count: int
    linestring_geometry_count: int
    node_count: int
    edge_count: int
    derived_node_count: int
    matched_endpoint_count: int
    unmatched_endpoint_count: int
    zero_length_edge_count: int
    self_loop_count: int
    duplicate_geometry_count: int
    connected_component_count: int
    largest_component_node_count: int
    largest_component_edge_count: int
    largest_component_node_percentage: float
    isolated_node_count: int
    issues: tuple[SpatialIssue, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "geometry_type_counts",
            MappingProxyType(dict(sorted(self.geometry_type_counts.items()))),
        )
        object.__setattr__(self, "issues", tuple(self.issues))


@dataclass(frozen=True, slots=True)
class PedestrianNetworkResult:
    """Routing-ready spatial tables and their processing report."""

    nodes: pd.DataFrame
    edges: pd.DataFrame
    report: SpatialProcessingReport
    geographic_crs: str
    projected_crs: str
    bidirectional: bool = True


@dataclass(frozen=True, slots=True)
class SnapDistanceReport:
    """Summary of nearest-node distances for one mapped dataset."""

    source_row_count: int
    mapped_count: int
    unmapped_count: int
    outside_threshold_count: int
    threshold_m: float
    minimum_distance_m: float | None
    median_distance_m: float | None
    percentile_95_distance_m: float | None
    maximum_distance_m: float | None


@dataclass(frozen=True, slots=True)
class SpatialMappingResult:
    """One complete source-to-node mapping and distance summary."""

    mappings: pd.DataFrame
    report: SnapDistanceReport


@dataclass(frozen=True, slots=True)
class SpatialWorkflowResult:
    """Processed network plus complete sensor and landmark node mappings."""

    network: PedestrianNetworkResult
    sensor_mapping: SpatialMappingResult
    landmark_mapping: SpatialMappingResult
    excluded_datasets: tuple[str, ...] = EXCLUDED_SPATIAL_DATASETS


@dataclass(frozen=True, slots=True)
class _ParsedGeometry:
    geometry_type: Literal["Point", "LineString"]
    coordinates: tuple[tuple[float, float], ...]
    source_row: int
    source_object_id: object
    source_network_id: object


@dataclass(frozen=True, slots=True)
class _NodeBuild:
    node_id: str
    source_object_id: object
    source_network_id: object
    longitude: float
    latitude: float
    projected_x: float
    projected_y: float
    node_origin: str


class _GeometryProblem(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _require_columns(frame: pd.DataFrame, required: Sequence[str], name: str) -> None:
    if not isinstance(frame, pd.DataFrame):
        raise TypeError(f"{name} must be a pandas DataFrame")
    missing = tuple(column for column in required if column not in frame.columns)
    if missing:
        raise SpatialProcessingError(f"{name} is missing required columns: {missing}")


def _trace_value(value: object) -> object:
    if value is None or value is pd.NA:
        return None
    if isinstance(value, np.generic):
        value = value.item()
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _stable_text(value: object) -> str:
    value = _trace_value(value)
    return "<null>" if value is None else str(value)


def _coordinate_text(value: float) -> str:
    return repr(float(value))


def _point_wkt(longitude: float, latitude: float) -> str:
    return f"POINT ({_coordinate_text(longitude)} {_coordinate_text(latitude)})"


def _line_wkt(coordinates: Sequence[tuple[float, float]]) -> str:
    values = ", ".join(
        f"{_coordinate_text(longitude)} {_coordinate_text(latitude)}"
        for longitude, latitude in coordinates
    )
    return f"LINESTRING ({values})"


def _coordinate_pair(value: object) -> tuple[float, float]:
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        raise _GeometryProblem(
            "invalid_coordinates", "coordinate must contain longitude and latitude"
        )
    if isinstance(value[0], bool) or isinstance(value[1], bool):
        raise _GeometryProblem("invalid_coordinates", "boolean coordinate is invalid")
    try:
        longitude = float(value[0])
        latitude = float(value[1])
    except (TypeError, ValueError) as error:
        raise _GeometryProblem(
            "invalid_coordinates", "coordinate values must be numeric"
        ) from error
    if not math.isfinite(longitude) or not math.isfinite(latitude):
        raise _GeometryProblem("invalid_coordinates", "coordinate must be finite")
    if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
        raise _GeometryProblem(
            "invalid_coordinates", "coordinate is outside WGS84 bounds"
        )
    return longitude, latitude


def _parse_geometry(
    raw_geometry: object,
    source_row: int,
    source_object_id: object,
    source_network_id: object,
) -> _ParsedGeometry:
    if raw_geometry is None or raw_geometry is pd.NA:
        raise _GeometryProblem("missing_geometry", "Geo Shape is missing")
    try:
        if pd.isna(raw_geometry):
            raise _GeometryProblem("missing_geometry", "Geo Shape is missing")
    except (TypeError, ValueError):
        pass
    if not isinstance(raw_geometry, str) or not raw_geometry.strip():
        raise _GeometryProblem("missing_geometry", "Geo Shape is blank")
    try:
        value = json.loads(raw_geometry)
    except (json.JSONDecodeError, TypeError) as error:
        raise _GeometryProblem("malformed_geometry", "Geo Shape is not valid JSON") from error
    if not isinstance(value, dict):
        raise _GeometryProblem("malformed_geometry", "Geo Shape must be a JSON object")
    geometry_type = value.get("type")
    coordinates = value.get("coordinates")
    if geometry_type == "Point":
        parsed = (_coordinate_pair(coordinates),)
    elif geometry_type == "LineString":
        if not isinstance(coordinates, list) or len(coordinates) < 2:
            raise _GeometryProblem(
                "invalid_coordinates", "LineString requires at least two coordinates"
            )
        parsed = tuple(_coordinate_pair(coordinate) for coordinate in coordinates)
    else:
        raise _GeometryProblem(
            "unsupported_geometry", f"unsupported geometry type: {geometry_type!r}"
        )
    return _ParsedGeometry(
        geometry_type=geometry_type,
        coordinates=parsed,
        source_row=source_row,
        source_object_id=_trace_value(source_object_id),
        source_network_id=_trace_value(source_network_id),
    )


def _projected_length(
    coordinates: Sequence[tuple[float, float]], transformer: Transformer
) -> float:
    x_values, y_values = transformer.transform(
        [value[0] for value in coordinates],
        [value[1] for value in coordinates],
    )
    return float(
        sum(
            math.hypot(x_values[index] - x_values[index - 1], y_values[index] - y_values[index - 1])
            for index in range(1, len(coordinates))
        )
    )


def _canonical_geometry_key(
    coordinates: tuple[tuple[float, float], ...]
) -> tuple[tuple[float, float], ...]:
    reversed_coordinates = tuple(reversed(coordinates))
    return min(coordinates, reversed_coordinates)


def _uuid(kind: str, *values: object) -> str:
    identity = "|".join(["cityflow-spatial", kind, *(_stable_text(value) for value in values)])
    return str(uuid5(NAMESPACE_URL, identity))


def _grid_cell(x_value: float, y_value: float, tolerance: float) -> tuple[int, int]:
    return math.floor(x_value / tolerance), math.floor(y_value / tolerance)


def _nearest_grid_node(
    x_value: float,
    y_value: float,
    nodes: Sequence[_NodeBuild],
    grid: Mapping[tuple[int, int], Sequence[int]],
    tolerance: float,
) -> int | None:
    cell_x, cell_y = _grid_cell(x_value, y_value, tolerance)
    candidates: list[tuple[float, str, int]] = []
    maximum_squared = tolerance * tolerance
    for offset_x in (-1, 0, 1):
        for offset_y in (-1, 0, 1):
            for index in grid.get((cell_x + offset_x, cell_y + offset_y), ()):
                node = nodes[index]
                distance_squared = (
                    (x_value - node.projected_x) ** 2
                    + (y_value - node.projected_y) ** 2
                )
                if distance_squared <= maximum_squared:
                    candidates.append((distance_squared, node.node_id, index))
    return min(candidates)[2] if candidates else None


def _empty_nodes() -> pd.DataFrame:
    return pd.DataFrame(columns=ROUTING_NODE_COLUMNS)


def _empty_edges() -> pd.DataFrame:
    return pd.DataFrame(columns=ROUTING_EDGE_COLUMNS)


def process_pedestrian_network(
    frame: pd.DataFrame,
    config: SpatialProcessingConfig | None = None,
) -> PedestrianNetworkResult:
    """Parse network GeoJSON and build deterministic routing tables.

    Pedestrian edges are assumed bidirectional because the source contains no
    direction restriction. ``cost`` and ``reverse_cost`` therefore both start
    as projected edge length; sensory-weighted costs belong to a later stage.
    Invalid source records are counted and retained as bounded report issues.
    """

    _require_columns(frame, NETWORK_SOURCE_COLUMNS, "pedestrian network")
    config = config or SpatialProcessingConfig()
    source = frame.loc[:, NETWORK_SOURCE_COLUMNS].copy(deep=True)
    transformer = Transformer.from_crs(
        config.geographic_crs, config.projected_crs, always_xy=True
    )

    geometry_types: Counter[str] = Counter()
    problem_counts: Counter[str] = Counter()
    issues: list[SpatialIssue] = []
    points: list[_ParsedGeometry] = []
    lines: list[_ParsedGeometry] = []

    for source_row, row in enumerate(source.itertuples(index=False, name=None)):
        _, raw_geometry, object_id, network_id = row
        try:
            geometry = _parse_geometry(
                raw_geometry, source_row, object_id, network_id
            )
            geometry_types[geometry.geometry_type] += 1
            if geometry.geometry_type == "Point":
                points.append(geometry)
            else:
                lines.append(geometry)
        except _GeometryProblem as error:
            problem_counts[error.code] += 1
            if len(issues) < config.max_reported_issues:
                issues.append(
                    SpatialIssue(
                        code=error.code,
                        source_row=source_row,
                        source_object_id=_trace_value(object_id),
                        message=str(error),
                    )
                )

    valid_lines: list[tuple[_ParsedGeometry, float]] = []
    zero_length_count = 0
    for line in lines:
        length = _projected_length(line.coordinates, transformer)
        if not math.isfinite(length) or length <= config.zero_length_tolerance_m:
            zero_length_count += 1
            if len(issues) < config.max_reported_issues:
                issues.append(
                    SpatialIssue(
                        "zero_length_edge",
                        line.source_row,
                        line.source_object_id,
                        "LineString has zero or negligible projected length",
                    )
                )
            continue
        valid_lines.append((line, length))

    point_occurrences: Counter[tuple[object, object, tuple[float, float]]] = Counter()
    point_entries: list[tuple[_ParsedGeometry, int]] = []
    for point in sorted(
        points,
        key=lambda value: (
            _stable_text(value.source_network_id),
            _stable_text(value.source_object_id),
            value.coordinates[0],
        ),
    ):
        key = (point.source_object_id, point.source_network_id, point.coordinates[0])
        occurrence = point_occurrences[key]
        point_occurrences[key] += 1
        point_entries.append((point, occurrence))

    nodes: list[_NodeBuild] = []
    source_grid: dict[tuple[int, int], list[int]] = defaultdict(list)
    for point, occurrence in point_entries:
        longitude, latitude = point.coordinates[0]
        projected_x, projected_y = transformer.transform(longitude, latitude)
        node_id = _uuid(
            "source-node",
            point.source_network_id,
            point.source_object_id,
            longitude,
            latitude,
            occurrence,
        )
        node = _NodeBuild(
            node_id,
            point.source_object_id,
            point.source_network_id,
            longitude,
            latitude,
            float(projected_x),
            float(projected_y),
            "source_point",
        )
        nodes.append(node)
        source_grid[
            _grid_cell(node.projected_x, node.projected_y, config.endpoint_tolerance_m)
        ].append(len(nodes) - 1)

    endpoint_occurrences: Counter[tuple[float, float]] = Counter()
    for line, _ in valid_lines:
        endpoint_occurrences[line.coordinates[0]] += 1
        endpoint_occurrences[line.coordinates[-1]] += 1

    endpoint_node: dict[tuple[float, float], str] = {}
    endpoint_origin: dict[tuple[float, float], str] = {}
    derived_grid: dict[tuple[int, int], list[int]] = defaultdict(list)
    for coordinate in sorted(endpoint_occurrences):
        longitude, latitude = coordinate
        projected_x, projected_y = transformer.transform(longitude, latitude)
        node_index = _nearest_grid_node(
            float(projected_x),
            float(projected_y),
            nodes,
            source_grid,
            config.endpoint_tolerance_m,
        )
        if node_index is None:
            node_index = _nearest_grid_node(
                float(projected_x),
                float(projected_y),
                nodes,
                derived_grid,
                config.endpoint_tolerance_m,
            )
        if node_index is None:
            node = _NodeBuild(
                _uuid("derived-node", longitude, latitude),
                None,
                None,
                longitude,
                latitude,
                float(projected_x),
                float(projected_y),
                "derived_endpoint",
            )
            nodes.append(node)
            node_index = len(nodes) - 1
            derived_grid[
                _grid_cell(
                    node.projected_x, node.projected_y, config.endpoint_tolerance_m
                )
            ].append(node_index)
        endpoint_node[coordinate] = nodes[node_index].node_id
        endpoint_origin[coordinate] = nodes[node_index].node_origin

    matched_endpoint_count = sum(
        count
        for coordinate, count in endpoint_occurrences.items()
        if endpoint_origin[coordinate] == "source_point"
    )
    unmatched_endpoint_count = sum(endpoint_occurrences.values()) - matched_endpoint_count

    duplicate_counts = Counter(
        _canonical_geometry_key(line.coordinates) for line, _ in valid_lines
    )
    line_occurrences: Counter[
        tuple[object, object, tuple[tuple[float, float], ...]]
    ] = Counter()
    edge_rows: list[dict[str, object]] = []
    self_loop_count = 0
    for line, length in sorted(
        valid_lines,
        key=lambda item: (
            _stable_text(item[0].source_object_id),
            _stable_text(item[0].source_network_id),
            item[0].coordinates,
        ),
    ):
        source_node_id = endpoint_node[line.coordinates[0]]
        target_node_id = endpoint_node[line.coordinates[-1]]
        if source_node_id == target_node_id:
            self_loop_count += 1
            if len(issues) < config.max_reported_issues:
                issues.append(
                    SpatialIssue(
                        "self_loop",
                        line.source_row,
                        line.source_object_id,
                        "LineString endpoints resolve to the same routing node",
                    )
                )
            continue
        canonical_key = _canonical_geometry_key(line.coordinates)
        key = (line.source_object_id, line.source_network_id, canonical_key)
        occurrence = line_occurrences[key]
        line_occurrences[key] += 1
        edge_rows.append(
            {
                "edge_id": _uuid(
                    "edge",
                    line.source_object_id,
                    line.source_network_id,
                    canonical_key,
                    occurrence,
                ),
                "source_object_id": line.source_object_id,
                "source_network_id": line.source_network_id,
                "source_node_id": source_node_id,
                "target_node_id": target_node_id,
                "length_m": length,
                "cost": length,
                "reverse_cost": length,
                "geometry_wkt": _line_wkt(line.coordinates),
                "duplicate_geometry": duplicate_counts[canonical_key] > 1,
            }
        )

    graph = nx.Graph()
    graph.add_nodes_from(node.node_id for node in nodes)
    graph.add_edges_from(
        (row["source_node_id"], row["target_node_id"]) for row in edge_rows
    )
    component_values: list[tuple[set[str], int]] = []
    for component in nx.connected_components(graph):
        edge_count = sum(
            row["source_node_id"] in component
            and row["target_node_id"] in component
            for row in edge_rows
        )
        component_values.append((set(component), edge_count))
    component_values.sort(
        key=lambda item: (-len(item[0]), -item[1], min(item[0]) if item[0] else "")
    )
    component_by_node: dict[str, int] = {}
    for component_id, (component, _) in enumerate(component_values, start=1):
        component_by_node.update({node_id: component_id for node_id in component})

    node_rows = [
        {
            "node_id": node.node_id,
            "source_object_id": node.source_object_id,
            "source_network_id": node.source_network_id,
            "longitude": node.longitude,
            "latitude": node.latitude,
            "geometry_wkt": _point_wkt(node.longitude, node.latitude),
            "component_id": component_by_node[node.node_id],
            "is_primary_component": component_by_node[node.node_id] == 1,
            "node_origin": node.node_origin,
        }
        for node in nodes
    ]
    for row in edge_rows:
        component_id = component_by_node[row["source_node_id"]]
        row["component_id"] = component_id
        row["is_primary_component"] = component_id == 1

    node_frame = (
        pd.DataFrame(node_rows, columns=ROUTING_NODE_COLUMNS)
        .sort_values("node_id", kind="mergesort")
        .reset_index(drop=True)
        if node_rows
        else _empty_nodes()
    )
    edge_frame = (
        pd.DataFrame(edge_rows, columns=ROUTING_EDGE_COLUMNS)
        .sort_values("edge_id", kind="mergesort")
        .reset_index(drop=True)
        if edge_rows
        else _empty_edges()
    )

    largest_nodes = len(component_values[0][0]) if component_values else 0
    largest_edges = component_values[0][1] if component_values else 0
    invalid_geometry_count = sum(problem_counts.values())
    report = SpatialProcessingReport(
        source_row_count=len(source),
        geometry_type_counts=geometry_types,
        valid_geometry_count=len(points) + len(lines),
        invalid_geometry_count=invalid_geometry_count,
        missing_geometry_count=problem_counts["missing_geometry"],
        malformed_geometry_count=problem_counts["malformed_geometry"],
        unsupported_geometry_count=problem_counts["unsupported_geometry"],
        invalid_coordinate_count=problem_counts["invalid_coordinates"],
        point_geometry_count=len(points),
        linestring_geometry_count=len(lines),
        node_count=len(node_frame),
        edge_count=len(edge_frame),
        derived_node_count=int(node_frame["node_origin"].eq("derived_endpoint").sum())
        if len(node_frame)
        else 0,
        matched_endpoint_count=matched_endpoint_count,
        unmatched_endpoint_count=unmatched_endpoint_count,
        zero_length_edge_count=zero_length_count,
        self_loop_count=self_loop_count,
        duplicate_geometry_count=int(edge_frame["duplicate_geometry"].sum())
        if len(edge_frame)
        else 0,
        connected_component_count=len(component_values),
        largest_component_node_count=largest_nodes,
        largest_component_edge_count=largest_edges,
        largest_component_node_percentage=(
            largest_nodes / len(node_frame) * 100.0 if len(node_frame) else 0.0
        ),
        isolated_node_count=nx.number_of_isolates(graph),
        issues=tuple(issues),
    )
    return PedestrianNetworkResult(
        nodes=node_frame,
        edges=edge_frame,
        report=report,
        geographic_crs=config.geographic_crs,
        projected_crs=config.projected_crs,
    )


def _mapping_report(
    distances: Sequence[float],
    source_count: int,
    threshold: float,
    unmapped_count: int,
) -> SnapDistanceReport:
    values = np.asarray(distances, dtype="float64")
    return SnapDistanceReport(
        source_row_count=source_count,
        mapped_count=len(values),
        unmapped_count=unmapped_count,
        outside_threshold_count=int(np.sum(values > threshold)),
        threshold_m=threshold,
        minimum_distance_m=float(np.min(values)) if len(values) else None,
        median_distance_m=float(np.quantile(values, 0.5, method="linear"))
        if len(values)
        else None,
        percentile_95_distance_m=float(np.quantile(values, 0.95, method="linear"))
        if len(values)
        else None,
        maximum_distance_m=float(np.max(values)) if len(values) else None,
    )


def _map_to_network(
    frame: pd.DataFrame,
    nodes: pd.DataFrame,
    *,
    id_column: str,
    output_columns: tuple[str, ...],
    threshold: float,
    config: SpatialProcessingConfig,
    dataset_name: str,
) -> SpatialMappingResult:
    _require_columns(frame, (id_column, "longitude", "latitude"), dataset_name)
    _require_columns(
        nodes,
        (
            "node_id",
            "longitude",
            "latitude",
            "component_id",
            "is_primary_component",
        ),
        "routing nodes",
    )
    source = frame.loc[:, (id_column, "longitude", "latitude")].copy(deep=True)
    transformer = Transformer.from_crs(
        config.geographic_crs, config.projected_crs, always_xy=True
    )
    usable_nodes = nodes.loc[
        :,
        (
            "node_id",
            "longitude",
            "latitude",
            "component_id",
            "is_primary_component",
        ),
    ].copy()
    if len(usable_nodes):
        node_x, node_y = transformer.transform(
            usable_nodes["longitude"].astype(float).to_numpy(),
            usable_nodes["latitude"].astype(float).to_numpy(),
        )
        node_x_values = np.asarray(node_x, dtype="float64")
        node_y_values = np.asarray(node_y, dtype="float64")
    else:
        node_x_values = np.asarray([], dtype="float64")
        node_y_values = np.asarray([], dtype="float64")

    rows: list[dict[str, object]] = []
    distances: list[float] = []
    unmapped_count = 0
    for identifier, longitude_raw, latitude_raw in source.itertuples(index=False, name=None):
        try:
            longitude, latitude = _coordinate_pair([longitude_raw, latitude_raw])
        except _GeometryProblem:
            longitude = latitude = float("nan")
        if not len(usable_nodes) or not math.isfinite(longitude) or not math.isfinite(latitude):
            unmapped_count += 1
            rows.append(
                {
                    id_column: identifier,
                    "node_id": pd.NA,
                    "snap_distance_m": float("nan"),
                    "within_snap_threshold": False,
                    "network_component_id": pd.NA,
                    "is_primary_component": False,
                }
            )
            continue
        point_x, point_y = transformer.transform(longitude, latitude)
        squared = (node_x_values - point_x) ** 2 + (node_y_values - point_y) ** 2
        nearest_index = int(np.argmin(squared))
        distance = float(math.sqrt(float(squared[nearest_index])))
        nearest = usable_nodes.iloc[nearest_index]
        distances.append(distance)
        rows.append(
            {
                id_column: identifier,
                "node_id": nearest["node_id"],
                "snap_distance_m": distance,
                "within_snap_threshold": distance <= threshold,
                "network_component_id": nearest["component_id"],
                "is_primary_component": bool(nearest["is_primary_component"]),
            }
        )
    mappings = pd.DataFrame(rows, columns=output_columns)
    if len(mappings):
        mappings = mappings.sort_values(id_column, kind="mergesort").reset_index(drop=True)
    return SpatialMappingResult(
        mappings=mappings,
        report=_mapping_report(
            distances, len(source), threshold, unmapped_count
        ),
    )


def map_sensors_to_network(
    sensors: pd.DataFrame,
    nodes: pd.DataFrame,
    config: SpatialProcessingConfig | None = None,
) -> SpatialMappingResult:
    """Map every transformed canonical sensor to its nearest routing node."""

    config = config or SpatialProcessingConfig()
    return _map_to_network(
        sensors,
        nodes,
        id_column="sensor_id",
        output_columns=SENSOR_MAPPING_COLUMNS,
        threshold=config.sensor_snap_threshold_m,
        config=config,
        dataset_name="canonical sensors",
    )


def map_landmarks_to_network(
    landmarks: pd.DataFrame,
    nodes: pd.DataFrame,
    config: SpatialProcessingConfig | None = None,
) -> SpatialMappingResult:
    """Map every transformed landmark to its nearest routing node."""

    config = config or SpatialProcessingConfig()
    return _map_to_network(
        landmarks,
        nodes,
        id_column="landmark_id",
        output_columns=LANDMARK_MAPPING_COLUMNS,
        threshold=config.landmark_snap_threshold_m,
        config=config,
        dataset_name="landmarks",
    )


def process_spatial_workflow(
    network: pd.DataFrame,
    canonical_sensors: pd.DataFrame,
    landmarks: pd.DataFrame,
    config: SpatialProcessingConfig | None = None,
) -> SpatialWorkflowResult:
    """Run network processing and complete sensor/landmark node mapping."""

    config = config or SpatialProcessingConfig()
    network_result = process_pedestrian_network(network, config)
    return SpatialWorkflowResult(
        network=network_result,
        sensor_mapping=map_sensors_to_network(
            canonical_sensors, network_result.nodes, config
        ),
        landmark_mapping=map_landmarks_to_network(
            landmarks, network_result.nodes, config
        ),
    )


__all__ = [
    "EXCLUDED_SPATIAL_DATASETS",
    "LANDMARK_MAPPING_COLUMNS",
    "NETWORK_SOURCE_COLUMNS",
    "ROUTING_EDGE_COLUMNS",
    "ROUTING_NODE_COLUMNS",
    "SENSOR_MAPPING_COLUMNS",
    "PedestrianNetworkResult",
    "SnapDistanceReport",
    "SpatialIssue",
    "SpatialMappingResult",
    "SpatialProcessingConfig",
    "SpatialProcessingError",
    "SpatialProcessingReport",
    "SpatialWorkflowResult",
    "map_landmarks_to_network",
    "map_sensors_to_network",
    "process_pedestrian_network",
    "process_spatial_workflow",
]

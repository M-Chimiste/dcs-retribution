from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterator, Optional
from uuid import UUID

from game.dcs.groundunittype import GroundUnitType
from game.theater.iadsnetwork.iadsrole import IadsRole
from game.theater.theatergroundobject import (
    IadsBuildingGroundObject,
    IadsGroundObject,
    NavalGroundObject,
    TheaterGroundObject,
)
from game.theater.theatergroup import IadsGroundGroup

if TYPE_CHECKING:
    from game.game import Game
    from game.sim import GameUpdateEvents


class IadsNetworkException(Exception):
    pass


@dataclass
class SkynetNode:
    """Dataclass for a SkynetNode used in the LUA Data table by the luagenerator"""

    dcs_name: str
    player: bool
    iads_role: IadsRole
    properties: dict[str, str] = field(default_factory=dict)
    connections: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))

    @staticmethod
    def dcs_name_for_group(group: IadsGroundGroup) -> str:
        if group.iads_role in [
            IadsRole.EWR,
            IadsRole.COMMAND_CENTER,
            IadsRole.CONNECTION_NODE,
            IadsRole.POWER_SOURCE,
        ]:
            # Use UnitName for EWR, CommandCenter, Comms, Power
            for unit in group.units:
                # Check for alive units in the group
                if unit.alive:
                    return unit.unit_name
            if group.units[0].is_static:
                # Statics will be placed as dead unit
                return group.units[0].unit_name
            # If no alive unit is available and not static raise error
            raise IadsNetworkException(f"{group.name} has no skynet usable units")
        else:
            # Use the GroupName for SAMs, SAMAsEWR and PDs
            return group.group_name

    @classmethod
    def from_group(cls, group: IadsGroundGroup) -> SkynetNode:
        node = cls(
            cls.dcs_name_for_group(group),
            group.ground_object.is_friendly(True),
            group.iads_role,
        )
        unit_type = group.units[0].unit_type
        if unit_type is not None and isinstance(unit_type, GroundUnitType):
            node.properties = unit_type.skynet_properties.to_dict()
        return node


class IadsNetworkNode:
    """IadsNetworkNode which particicpates to the IADS Network and has connections to Power Sources, Comms or Point Defenses. A network node can be a SAM System, EWR or Command Center"""

    def __init__(self, group: IadsGroundGroup) -> None:
        self.group = group
        self.connections: dict[UUID, IadsGroundGroup] = {}

    def __str__(self) -> str:
        return self.group.group_name

    def add_connection_for_tgo(self, tgo: TheaterGroundObject) -> None:
        """Add all possible connections for the given TGO to the node"""
        for group in tgo.groups:
            if isinstance(group, IadsGroundGroup) and group.iads_role.participate:
                self.add_connection_for_group(group)

    def add_connection_for_group(self, group: IadsGroundGroup) -> None:
        """Add connection for the given GroundGroup with unique ID"""
        self.connections[uuid.uuid4()] = group


class IadsNetwork:
    """IADS Network consisting of multiple Network nodes and connections. The Network represents all possible connections of ground objects regardless if a tgo is under control of red or blue. The network can run in either advanced or basic mode. The advanced network can be created by a given configuration in the campaign yaml or computed by Range. The basic mode is a fallback mode which does not use Comms, Power or Command Centers. The network will be used to visualize all connections at the map and for creating the needed Lua data for the skynet plugin"""

    def __init__(
        self, advanced: bool, iads_data: list[str | dict[str, list[str]]]
    ) -> None:
        self.advanced_iads = advanced
        self.ground_objects: dict[str, TheaterGroundObject] = {}
        self.nodes: list[IadsNetworkNode] = []
        self.iads_config: dict[str, list[str]] = defaultdict(list)

        # Load Iads config from the campaign data
        for element in iads_data:
            if isinstance(element, str):
                self.iads_config[element] = []
            elif isinstance(element, dict):
                for iads_node, iads_connections in element.items():
                    self.iads_config[iads_node] = iads_connections
            else:
                raise RuntimeError("Invalid iads_config in campaign")

    def skynet_nodes(self, game: Game) -> list[SkynetNode]:
        """Get all skynet nodes from the IADS Network"""
        skynet_nodes: list[SkynetNode] = []
        for node in self.nodes:
            if game.iads_considerate_culling(node.group.ground_object):
                # Skip culled ground objects
                continue

            all_dead = not any([x.alive for x in node.group.units])
            if all_dead:
                continue

            # SkynetNode.from_group(node.group) may raise an exception
            #  (originating from SkynetNode.dcs_name_for_group)
            # but if it does, we want to know because it's supposed to be impossible afaict
            skynet_node = SkynetNode.from_group(node.group)
            for connection in node.connections.values():
                if not any([x.alive for x in connection.units]):
                    continue
                if connection.ground_object.is_friendly(
                    skynet_node.player
                ) and not game.iads_considerate_culling(connection.ground_object):
                    skynet_node.connections[connection.iads_role.value].append(
                        SkynetNode.dcs_name_for_group(connection)
                    )
            skynet_nodes.append(skynet_node)
        return skynet_nodes

    def _update_iads_comms_and_power(
        self, tgo: TheaterGroundObject, events: GameUpdateEvents
    ) -> None:
        assert self.advanced_iads, "_update_iads_comms_and_power requires advanced IADS"
        is_comm_or_power = IadsRole.for_category(tgo.category).is_comms_or_power
        assert is_comm_or_power, "Invalid TGO was given for _update_iads_building"

        """ 
        Delete/Update connections to the comm tower/power station
        If this function is called, it should imply only 2 possibilities (unless I missed one):
            1) A capture event occurred, thus the building now belongs to the capturing team
                => All connections to this building are to the enemy, thus delete them
                 (mind that no new connections could have been formed since all TGOs were depopulated)
            2) The building was destroyed during a mission
                => In this case we don't need to delete the connections
                    instead we just update the nodes that connect to this building
                    because those connections are still coming from friendly TGOs
        Given the above, we should be able to use the following implications:
            If the building is friendly compared to the node that we're checking
                => Building was destroyed during mission
                => Update nodes that connect to this building
            Otherwise if the building is not friendly compared to the node we're checking
                => Capture event occurred
                => Delete all connections to this building

        TODO: clean up the code below by wrapping comm towers and power stations
                preferably in a class like IadsNetworkNode, keeping a reference to connected nodes
        """
        for node in self.nodes:
            to_delete = []
            for cID in node.connections:
                group = node.connections[cID]
                if group.ground_object is tgo:
                    if self._is_friendly(node, tgo):
                        events.update_iads_node(node)
                    else:
                        to_delete.append(cID)
                        events.delete_iads_connection(cID)
            for cID in to_delete:
                del node.connections[cID]
        if not self.iads_config:
            self._update_network(tgo, events)

    def update_tgo(self, tgo: TheaterGroundObject, events: GameUpdateEvents) -> None:
        """Update the IADS Network for the given TGO"""
        if self.advanced_iads and IadsRole.for_category(tgo.category).is_comms_or_power:
            return self._update_iads_comms_and_power(tgo, events)
        # Remove existing nodes for the given tgo
        for cn in self.nodes:
            if cn.group.ground_object == tgo:
                self.nodes.remove(cn)
                for cID in cn.connections:
                    events.delete_iads_connection(cID)

        node = self.node_for_tgo(tgo)
        if node is None:
            # Not participating
            return
        events.update_iads_node(node)
        if self.advanced_iads:
            if self.iads_config:
                self._add_connections_from_config(node)
            else:
                self._make_advanced_connections_by_range(node)

    def node_for_group(self, group: IadsGroundGroup) -> IadsNetworkNode:
        """Get existing node from the iads network or create a new node"""
        for cn in self.nodes:
            if cn.group == group:
                return cn

        node = IadsNetworkNode(group)
        self.nodes.append(node)
        return node

    def node_for_tgo(self, tgo: TheaterGroundObject) -> Optional[IadsNetworkNode]:
        """Get existing node from the iads network or create a new node"""
        for cn in self.nodes:
            if cn.group.ground_object == tgo:
                return cn

        # Create new connection_node if none exists
        node: Optional[IadsNetworkNode] = None
        for group in tgo.groups:
            # TODO Cleanup
            if isinstance(group, IadsGroundGroup) and group.alive_units > 0:
                # The first IadsGroundGroup is always the primary Group
                if not node and group.iads_role.participate:
                    # Primary Node
                    node = self.node_for_group(group)
                elif node and group.iads_role == IadsRole.POINT_DEFENSE:
                    # Point Defense Node for this TGO
                    node.add_connection_for_group(group)

        if node is None:
            logging.debug(f"TGO {tgo.name} not participating to IADS")
        return node

    def initialize_network(self, ground_objects: Iterator[TheaterGroundObject]) -> None:
        """Initialize the IADS network in advanced or basic mode depending on the campaign"""
        for tgo in ground_objects:
            self.ground_objects[tgo.original_name] = tgo
        if self.advanced_iads:
            # Advanced mode
            if self.iads_config:
                # Load from Configuration File
                self.initialize_network_from_config()
            else:
                # Load from Range
                self.initialize_network_from_range()

        # basic mode if no advanced iads support or network init created no connections
        if not self.nodes:
            self.initialize_basic_iads()

    def initialize_basic_iads(self) -> None:
        """Initialize the IADS Network in basic mode (SAM & EWR only)"""
        for go in self.ground_objects.values():
            if isinstance(go, IadsGroundObject):
                self.node_for_tgo(go)

    def initialize_network_from_config(self) -> None:
        """Initialize the IADS Network from a configuration"""
        for primary_node in self.iads_config.keys():
            warning_msg = (
                f"IADS: No ground object found for {primary_node}."
                f" This can be normal behaviour."
            )
            if primary_node in self.ground_objects:
                node = self.node_for_tgo(self.ground_objects[primary_node])
            else:
                node = None
                warning_msg = (
                    f"IADS: No ground object found for connection {primary_node}"
                )

            if node is None:
                # Log a warning as this can be normal. Possible case is for example
                # when the campaign request a Long Range SAM but the faction has none
                # available. Therefore the TGO will not get populated at all
                logging.warning(warning_msg)
                continue
            self._add_connections_from_config(node)

    def _add_connections_from_config(self, node: IadsNetworkNode) -> None:
        """Add all connections for the given primary node based on the iads_config"""
        primary_node = node.group.ground_object.original_name
        connections = self.iads_config[primary_node]
        for secondary_node in connections:
            try:
                node.add_connection_for_tgo(self.ground_objects[secondary_node])
            except KeyError:
                logging.error(
                    f"IADS: No ground object found for connection {secondary_node}"
                )
                continue

    def initialize_network_from_range(self) -> None:
        """Initialize the IADS Network by range"""
        for go in self.ground_objects.values():
            is_iads_go = isinstance(go, IadsGroundObject)
            is_iads_sea = isinstance(go, NavalGroundObject)
            is_iads_cc = isinstance(go, IadsBuildingGroundObject)
            is_iads_cc &= IadsRole.for_category(go.category) == IadsRole.COMMAND_CENTER
            if is_iads_go or is_iads_sea or is_iads_cc:
                # Set as primary node
                node = self.node_for_tgo(go)
                if node is None:
                    # TGO does not participate to iads network
                    continue
                self._make_advanced_connections_by_range(node)

    def _is_friendly(self, node: IadsNetworkNode, tgo: TheaterGroundObject) -> bool:
        node_friendly = node.group.ground_object.is_friendly(True)
        tgo_friendly = tgo.is_friendly(True)
        return node_friendly == tgo_friendly

    def _update_network(
        self, tgo: TheaterGroundObject, events: GameUpdateEvents
    ) -> None:
        if tgo.is_dead:
            return
        iads_role = IadsRole.for_category(tgo.category)
        if not iads_role.is_comms_or_power:
            return
        for node in self.nodes:
            dist = node.group.ground_object.position.distance_to_point(tgo.position)
            in_range = dist < iads_role.connection_range.meters
            if in_range and self._is_friendly(node, tgo):
                node.add_connection_for_tgo(tgo)
                events.update_iads_node(node)

    def _make_advanced_connections_by_range(self, node: IadsNetworkNode) -> None:
        tgo = node.group.ground_object
        # Find nearby Power or Connection
        for nearby_go in self.ground_objects.values():
            iads_role = IadsRole.for_category(nearby_go.category)
            if not iads_role.is_comms_or_power or nearby_go == tgo:
                continue
            dist = nearby_go.position.distance_to_point(tgo.position)
            in_range = dist <= iads_role.connection_range.meters
            if in_range and self._is_friendly(node, nearby_go):
                node.add_connection_for_tgo(nearby_go)

"""
Structure Permissions Plugin for Endstone
Allows non-operator players with specific permissions to use structure blocks
via temporary operator grant with command blocking.
"""

import json
import time
from pathlib import Path
from typing import Dict, Set

from endstone.command import Command, CommandSender
from endstone.plugin import Plugin
from endstone.event import (
    event_handler,
    PlayerInteractEvent,
    PlayerCommandEvent,
    PlayerMoveEvent,
    PlayerQuitEvent,
)


class StructurePermissionsPlugin(Plugin):
    
    api_version = "0.11"
    
    # Command definitions
    commands = {
        "sp": {
            "description": "Structure Permissions management",
            "usages": [
                "/sp grant <player: str>",
                "/sp revoke <player: str>",
                "/sp list"
            ],
            "permissions": ["structureperm.admin"]
        }
    }
    
    # Permission definitions
    permissions = {
        "structureperm.admin": {
            "description": "Manage structure permissions",
            "default": "op"
        }
    }
    
    # Configuration
    TIMEOUT_SECONDS = 120  # Auto-revoke after this many seconds
    MOVE_THRESHOLD = 3   # Revoke if player moves this many blocks away
    DATA_DIR = "plugins/structure_permissions_data"
    
    def __init__(self):
        super().__init__()
        # Track active temp-op sessions: player_name -> session_data
        self.active_sessions: Dict[str, dict] = {}
        # Track players with structure permission
        self.permitted_players: Set[str] = set()
    
    def on_load(self) -> None:
        # Ensure data directory exists
        Path(self.DATA_DIR).mkdir(parents=True, exist_ok=True)
        self._load_permitted_players()
    
    def on_enable(self) -> None:
        self.register_events(self)
        self.logger.info("Structure Permissions enabled")
        self.logger.info(f"Permitted players: {len(self.permitted_players)}")
    
    def on_disable(self) -> None:
        # Cleanup: revoke all active sessions
        for player_name in list(self.active_sessions.keys()):
            self._revoke_temp_op(player_name)
        self._save_permitted_players()
        self.logger.info("Structure Permissions disabled")
    
    # -------------------------------------------------------------------------
    # Data Persistence
    # -------------------------------------------------------------------------
    
    def _load_permitted_players(self) -> None:
        """Load permitted players from JSON file."""
        data_file = Path(self.DATA_DIR) / "permitted_players.json"
        if data_file.exists():
            try:
                with open(data_file, "r") as f:
                    self.permitted_players = set(json.load(f))
            except Exception as e:
                self.logger.warning(f"Failed to load permitted players: {e}")
                self.permitted_players = set()
        else:
            self.permitted_players = set()
    
    def _save_permitted_players(self) -> None:
        """Save permitted players to JSON file."""
        data_file = Path(self.DATA_DIR) / "permitted_players.json"
        try:
            with open(data_file, "w") as f:
                json.dump(list(self.permitted_players), f, indent=2)
        except Exception as e:
            self.logger.error(f"Failed to save permitted players: {e}")
    
    # -------------------------------------------------------------------------
    # Command Handler
    # -------------------------------------------------------------------------
    
    def on_command(self, sender: CommandSender, command: Command, args: list[str]) -> bool:
        if command.name.lower() != "sp":
            return False
        
        if not args:
            sender.send_message("§eUsage: /sp <grant|revoke|list> [player]")
            return True
        
        subcommand = args[0].lower()
        
        if subcommand == "grant":
            if len(args) < 2:
                sender.send_message("§cUsage: /sp grant <player>")
                return True
            player_name = args[1]
            self.permitted_players.add(player_name)
            self._save_permitted_players()
            sender.send_message(f"§aGranted structure block permission to {player_name}")
            
            # Notify player if online
            player = self.server.get_player(player_name)
            if player:
                player.send_message("§aYou now have permission to use structure blocks!")
            return True
        
        elif subcommand == "revoke":
            if len(args) < 2:
                sender.send_message("§cUsage: /sp revoke <player>")
                return True
            player_name = args[1]
            if player_name in self.permitted_players:
                self.permitted_players.discard(player_name)
                self._save_permitted_players()
                sender.send_message(f"§eRevoked structure block permission from {player_name}")
            else:
                sender.send_message(f"§c{player_name} doesn't have structure permissions")
            return True
        
        elif subcommand == "list":
            if not self.permitted_players:
                sender.send_message("§7No players have structure permissions.")
            else:
                sender.send_message("§aPlayers with structure permissions:")
                for name in sorted(self.permitted_players):
                    sender.send_message(f"§7- {name}")
            return True
        
        else:
            sender.send_message("§eUsage: /sp <grant|revoke|list> [player]")
            return True
    
    # -------------------------------------------------------------------------
    # Event Handlers
    # -------------------------------------------------------------------------
    
    @event_handler
    def on_player_interact(self, event: PlayerInteractEvent) -> None:
        """Grant temp op when a permitted player interacts with or places a structure block."""
        # Only handle right-click actions
        action = str(event.action)
        if "RIGHT_CLICK" not in action:
            return
        
        player = event.player
        
        # Already an operator - allow normal access
        if player.is_op and player.name not in self.active_sessions:
            return
        
        # Already in an active session - allow continued access
        if player.name in self.active_sessions:
            return
        
        # Check if player is interacting with an existing structure block
        is_clicking_structure_block = False
        if event.has_block:
            block_type = str(event.block.type).lower()
            if "structure_block" in block_type:
                is_clicking_structure_block = True
        
        # Check if player is holding a structure block to place
        is_holding_structure_block = False
        if event.has_item:
            item_type = str(event.item.type).lower()
            if "structure_block" in item_type:
                is_holding_structure_block = True
        
        # If neither, ignore this event
        if not is_clicking_structure_block and not is_holding_structure_block:
            return
        
        # Check if player is in permitted list
        if player.name not in self.permitted_players:
            event.cancel()
            player.send_message("§cYou don't have permission to use structure blocks.")
            player.send_message("§7Ask an operator to run: /sp grant " + player.name)
            return
        
        # Grant temporary operator status
        block_loc = event.block.location if event.has_block else player.location
        self._grant_temp_op(player, block_loc)
        
        # Cancel this event - the op grant happens too late for the current action
        # Player needs to click again (now that they're op, it will work)
        event.cancel()
        player.send_message("§7Click again to place/use the structure block.")
    
    @event_handler
    def on_player_command(self, event: PlayerCommandEvent) -> None:
        """Block ALL commands during an active temp-op session."""
        if event.player.name in self.active_sessions:
            event.cancel()
            event.player.send_message("§cCommands are disabled while using structure blocks.")
    
    @event_handler
    def on_player_move(self, event: PlayerMoveEvent) -> None:
        """Revoke temp op if player moves too far from the structure block."""
        player = event.player
        if player.name not in self.active_sessions:
            return
        
        session = self.active_sessions[player.name]
        block_pos = session["block_pos"]
        
        # Calculate distance from structure block
        loc = player.location
        dx = abs(loc.x - block_pos[0])
        dy = abs(loc.y - block_pos[1])
        dz = abs(loc.z - block_pos[2])
        
        # Revoke if moved beyond threshold
        if dx > self.MOVE_THRESHOLD or dy > self.MOVE_THRESHOLD or dz > self.MOVE_THRESHOLD:
            player.send_message("§eMoved away from structure block - access revoked.")
            self._revoke_temp_op(player.name)
    
    @event_handler
    def on_player_quit(self, event: PlayerQuitEvent) -> None:
        """Clean up session when player disconnects."""
        player_name = event.player.name
        if player_name in self.active_sessions:
            self._revoke_temp_op(player_name)
    
    # -------------------------------------------------------------------------
    # Session Management
    # -------------------------------------------------------------------------
    
    def _grant_temp_op(self, player, block_loc) -> None:
        """Grant temporary operator status to a player using /op command."""
        self.active_sessions[player.name] = {
            "original_op": player.is_op,
            "granted_at": time.time(),
            "block_pos": (int(block_loc.x), int(block_loc.y), int(block_loc.z))
        }
        
        # Use /op command to grant server's configured permission level (usually 2)
        # is_op property only grants level 1, but structure blocks need level 2
        self.server.dispatch_command(
            self.server.command_sender,
            f'op "{player.name}"'
        )
        
        player.send_message("§aTemporary structure block access granted.")
        player.send_message("§7Commands are disabled. Move away or wait to end session.")
        
        # Schedule auto-revoke
        self.server.scheduler.run_task(
            self,
            lambda: self._timeout_revoke(player.name),
            delay=self.TIMEOUT_SECONDS * 20  # Convert seconds to ticks
        )
        
        self.logger.info(f"Granted temp-op to {player.name} for structure block access")
    
    def _timeout_revoke(self, player_name: str) -> None:
        """Called by scheduler - revoke if session still active."""
        if player_name in self.active_sessions:
            player = self.server.get_player(player_name)
            if player:
                player.send_message("§eStructure block session timed out.")
            self._revoke_temp_op(player_name)
    
    def _revoke_temp_op(self, player_name: str) -> None:
        """Revoke temporary operator status from a player using /deop command."""
        if player_name not in self.active_sessions:
            return
        
        session = self.active_sessions.pop(player_name)
        player = self.server.get_player(player_name)
        
        # Only deop if they weren't originally an op
        if not session["original_op"]:
            self.server.dispatch_command(
                self.server.command_sender,
                f'deop "{player_name}"'
            )
        
        if player:
            player.send_message("§eStructure block access revoked.")
        
        self.logger.info(f"Revoked temp-op from {player_name}")


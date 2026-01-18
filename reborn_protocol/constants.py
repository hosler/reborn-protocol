"""
reborn_protocol.constants - Packet IDs and property constants

Defines all packet identifiers for client->server (PLI) and server->client (PLO)
communication, plus player/NPC property IDs and other protocol enums.

Based on GServer-v2 IEnums.h, TAccount.h, TNPC.h, TLevelBaddy.h, TLevelItem.h
"""
from enum import IntEnum, IntFlag


# =============================================================================
# PLI - Client -> Server Packet IDs (Player Input)
# =============================================================================
class PLI(IntEnum):
    """Client -> Server packet IDs (Player Input)"""
    LEVELWARP = 0              # Warp to level
    BOARDMODIFY = 1            # Modify board tile
    PLAYERPROPS = 2            # Send player properties
    NPCPROPS = 3               # Send NPC properties
    BOMBADD = 4                # Add bomb
    BOMBDEL = 5                # Remove bomb
    TOALL = 6                  # Chat message
    HORSEADD = 7               # Add/mount horse
    HORSEDEL = 8               # Remove horse
    ARROWADD = 9               # Add arrow
    FIRESPY = 10               # Fire spy (old)
    THROWCARRIED = 11          # Throw carried object
    ITEMADD = 12               # Add item to level
    ITEMDEL = 13               # Remove item
    CLAIMPKER = 14             # Claim PK
    BADDYPROPS = 15            # Baddy properties
    BADDYHURT = 16             # Hurt baddy
    BADDYADD = 17              # Add baddy
    FLAGSET = 18               # Set flag
    FLAGDEL = 19               # Delete flag
    OPENCHEST = 20             # Open chest
    PUTNPC = 21                # Put NPC
    NPCDEL = 22                # Delete NPC
    WANTFILE = 23              # Request file
    SHOWIMG = 24               # Show image/level chat
    UNKNOWN25 = 25             # Unknown
    HURTPLAYER = 26            # Hurt player
    EXPLOSION = 27             # Bomb explosion
    PRIVATEMESSAGE = 28        # Private message
    NPCWEAPONDEL = 29          # Delete NPC weapon
    LEVELWARPMOD = 30          # Level warp (modified)
    PACKETCOUNT = 31           # Packet count
    ITEMTAKE = 32              # Take item
    WEAPONADD = 33             # Add weapon
    UPDATEFILE = 34            # Update file
    ADJACENTLEVEL = 35         # Request adjacent level
    HITOBJECTS = 36            # Hit objects
    LANGUAGE = 37              # Set language
    TRIGGERACTION = 38         # Trigger action
    MAPINFO = 39               # Map info
    SHOOT = 40                 # Shoot (old format)
    SERVERWARP = 41            # Server warp
    MUTEPLAYER = 43            # Mute/unmute player
    PROCESSLIST = 44           # Process list
    UNKNOWN46 = 46             # Unknown (player count for gmap?)
    VERIFYWANTSEND = 47        # Verify file checksum and send if outdated
    SHOOT2 = 48                # Shoot (new format)
    RAWDATA = 50               # Raw data

    # RC (Remote Control) packets - 51-98
    RC_SERVEROPTIONSGET = 51   # Get server options
    RC_SERVEROPTIONSSET = 52   # Set server options
    RC_FOLDERCONFIGGET = 53    # Get folder config
    RC_FOLDERCONFIGSET = 54    # Set folder config
    RC_RESPAWNSET = 55         # Set respawn time
    RC_HORSELIFESET = 56       # Set horse lifetime
    RC_APINCREMENTSET = 57     # Set AP increment
    RC_BADDYRESPAWNSET = 58    # Set baddy respawn time
    RC_PLAYERPROPSGET = 59     # Get player props
    RC_PLAYERPROPSSET = 60     # Set player props
    RC_DISCONNECTPLAYER = 61   # Disconnect player
    RC_UPDATELEVELS = 62       # Update levels
    RC_ADMINMESSAGE = 63       # Admin message
    RC_PRIVADMINMESSAGE = 64   # Private admin message
    RC_LISTRCS = 65            # List RC users
    RC_DISCONNECTRC = 66       # Disconnect RC
    RC_APPLYREASON = 67        # Apply ban/mute reason
    RC_SERVERFLAGSGET = 68     # Get server flags
    RC_SERVERFLAGSSET = 69     # Set server flags
    RC_ACCOUNTADD = 70         # Add account
    RC_ACCOUNTDEL = 71         # Delete account
    RC_ACCOUNTLISTGET = 72     # Get account list
    RC_PLAYERPROPSGET2 = 73    # Get player props by ID
    RC_PLAYERPROPSGET3 = 74    # Get player props by account name
    RC_PLAYERPROPSRESET = 75   # Reset player props
    RC_PLAYERPROPSSET2 = 76    # Set player props (alt)
    RC_ACCOUNTGET = 77         # Get account
    RC_ACCOUNTSET = 78         # Set account
    RC_CHAT = 79               # RC chat
    PROFILEGET = 80            # Get profile
    PROFILESET = 81            # Set profile
    RC_WARPPLAYER = 82         # Warp player
    RC_PLAYERRIGHTSGET = 83    # Get player rights
    RC_PLAYERRIGHTSSET = 84    # Set player rights
    RC_PLAYERCOMMENTSGET = 85  # Get player comments
    RC_PLAYERCOMMENTSSET = 86  # Set player comments
    RC_PLAYERBANGET = 87       # Get player ban
    RC_PLAYERBANSET = 88       # Set player ban
    RC_FILEBROWSER_START = 89  # Start file browser
    RC_FILEBROWSER_CD = 90     # Change directory
    RC_FILEBROWSER_END = 91    # End file browser
    RC_FILEBROWSER_DOWN = 92   # Download file
    RC_FILEBROWSER_UP = 93     # Upload file
    NPCSERVERQUERY = 94        # NPC server query
    RC_FILEBROWSER_MOVE = 96   # Move file
    RC_FILEBROWSER_DELETE = 97 # Delete file
    RC_FILEBROWSER_RENAME = 98 # Rename file

    # NC (NPC Control) packets - 103-119, 150-151
    NC_NPCGET = 103            # Get NPC
    NC_NPCDELETE = 104         # Delete NPC
    NC_NPCRESET = 105          # Reset NPC
    NC_NPCSCRIPTGET = 106      # Get NPC script
    NC_NPCWARP = 107           # Warp NPC
    NC_NPCFLAGSGET = 108       # Get NPC flags
    NC_NPCSCRIPTSET = 109      # Set NPC script
    NC_NPCFLAGSSET = 110       # Set NPC flags
    NC_NPCADD = 111            # Add NPC
    NC_CLASSEDIT = 112         # Edit class
    NC_CLASSADD = 113          # Add class
    NC_LOCALNPCSGET = 114      # Get level NPCs
    NC_WEAPONLISTGET = 115     # Get weapon list
    NC_WEAPONGET = 116         # Get weapon
    NC_WEAPONADD = 117         # Add weapon
    NC_WEAPONDELETE = 118      # Delete weapon
    NC_CLASSDELETE = 119       # Delete class

    REQUESTUPDATEBOARD = 130   # Request board update

    NC_LEVELLISTGET = 150      # Get level list
    NC_LEVELLISTSET = 151      # Set level list

    REQUESTTEXT = 152          # Get server variable
    SENDTEXT = 154             # Set server variable

    RC_LARGEFILESTART = 155    # Start large file transfer
    RC_LARGEFILEEND = 156      # End large file transfer

    UPDATEGANI = 157           # Update gani
    UPDATESCRIPT = 158         # Request script
    UPDATEPACKAGEREQUESTFILE = 159  # Request package file
    RC_FOLDERDELETE = 160      # Delete folder
    UPDATECLASS = 161          # Class request
    RC_UNKNOWN162 = 162        # Unknown RC packet


# =============================================================================
# PLO - Server -> Client Packet IDs (Player Output)
# =============================================================================
class PLO(IntEnum):
    """Server -> Client packet IDs (Player Output)"""
    LEVELBOARD = 0             # Level board data
    LEVELLINK = 1              # Level link/warp
    BADDYPROPS = 2             # Baddy properties
    NPCPROPS = 3               # NPC properties
    LEVELCHEST = 4             # Level chest
    LEVELSIGN = 5              # Level sign
    LEVELNAME = 6              # Level name
    BOARDMODIFY = 7            # Board modification
    OTHERPLPROPS = 8           # Other player properties
    PLAYERPROPS = 9            # Player properties
    ISLEADER = 10              # Guild leader status
    BOMBADD = 11               # Bomb added
    BOMBDEL = 12               # Bomb deleted
    TOALL = 13                 # Chat message
    PLAYERWARP = 14            # Player warp
    WARPFAILED = 15            # Warp failed
    DISCMESSAGE = 16           # Disconnect message
    HORSEADD = 17              # Horse added
    HORSEDEL = 18              # Horse deleted
    ARROWADD = 19              # Arrow added
    FIRESPY = 20               # Fire spy
    THROWCARRIED = 21          # Throw carried
    ITEMADD = 22               # Item added
    ITEMDEL = 23               # Item deleted
    NPCMOVED = 24              # NPC moved (hides NPC for warps)
    SIGNATURE = 25             # Server signature
    NPCACTION = 26             # NPC action (unhandled by 6.037)
    BADDYHURT = 27             # Baddy hurt
    FLAGSET = 28               # Flag set
    NPCDEL = 29                # NPC deleted
    FILESENDFAILED = 30        # File send failed
    FLAGDEL = 31               # Flag deleted
    SHOWIMG = 32               # Show image
    NPCWEAPONADD = 33          # Weapon added
    NPCWEAPONDEL = 34          # Weapon deleted
    RC_ADMINMESSAGE = 35       # Admin message
    EXPLOSION = 36             # Explosion
    PRIVATEMESSAGE = 37        # Private message
    PUSHAWAY = 38              # Push/knockback
    LEVELMODTIME = 39          # Level modification time
    HURTPLAYER = 40            # Hurt player
    STARTMESSAGE = 41          # Start message (unhandled by 6.037)
    NEWWORLDTIME = 42          # World time/heartbeat
    DEFAULTWEAPON = 43         # Default weapon
    HASNPCSERVER = 44          # Has NPC server flag (unhandled by 5.07+)
    FILEUPTODATE = 45          # File is up to date
    HITOBJECTS = 46            # Hit objects
    STAFFGUILDS = 47           # Staff guilds list
    TRIGGERACTION = 48         # Trigger action
    PLAYERWARP2 = 49           # Player warp (GMAP)
    RC_ACCOUNTADD = 50         # Account added (deprecated)
    RC_ACCOUNTSTATUS = 51      # Account status (deprecated)
    RC_ACCOUNTNAME = 52        # Account name (deprecated)
    RC_ACCOUNTDEL = 53         # Account deleted (deprecated)
    RC_ACCOUNTPROPS = 54       # Account props (deprecated)
    ADDPLAYER = 55             # Add player (unhandled by 5.07+)
    DELPLAYER = 56             # Delete player (unhandled by 5.07+)
    RC_ACCOUNTPROPSGET = 57    # Account props get (deprecated)
    RC_ACCOUNTCHANGE = 58      # Account change (deprecated)
    RC_PLAYERPROPSCHANGE = 59  # Player props change (deprecated)
    UNKNOWN60 = 60             # Unknown (unhandled by 5.07+)
    RC_SERVERFLAGSGET = 61     # Server flags get
    RC_PLAYERRIGHTSGET = 62    # Player rights get
    RC_PLAYERCOMMENTSGET = 63  # Player comments get
    RC_PLAYERBANGET = 64       # Player ban get
    RC_FILEBROWSER_DIRLIST = 65  # File browser dir list
    RC_FILEBROWSER_DIR = 66    # File browser dir
    RC_FILEBROWSER_MESSAGE = 67  # File browser message
    LARGEFILESTART = 68        # Large file start
    LARGEFILEEND = 69          # Large file end
    RC_ACCOUNTLISTGET = 70     # Account list get
    RC_PLAYERPROPS = 71        # Player props (deprecated)
    RC_PLAYERPROPSGET = 72     # Player props get
    RC_ACCOUNTGET = 73         # Account get
    RC_CHAT = 74               # RC chat
    PROFILE = 75               # Profile (unhandled by 6.037)
    RC_SERVEROPTIONSGET = 76   # Server options get
    RC_FOLDERCONFIGGET = 77    # Folder config get
    NC_CONTROL = 78            # NC control (hijacked by GR)
    NPCSERVERADDR = 79         # NPC server address
    NC_LEVELLIST = 80          # Level list
    UNKNOWN81 = 81             # Unknown
    SERVERTEXT = 82            # Server text response
    UNKNOWN83 = 83             # Unknown
    LARGEFILESIZE = 84         # Large file size
    RAWDATA = 100              # Raw data size
    BOARDPACKET = 101          # Board packet (8192 bytes)
    FILE = 102                 # File transfer
    RC_MAXUPLOADFILESIZE = 103 # Max upload file size
    UNKNOWN104 = 104           # Unknown (unique code in 5.07/6.037)
    UPDATEPACKAGESIZE = 105    # Update package size
    UPDATEPACKAGEDONE = 106    # Update package done
    BOARDLAYER = 107           # Extra board layer
    UNKNOWN109 = 109           # Unknown
    UNKNOWN111 = 111           # Unknown
    UNKNOWN124 = 124           # Unknown (RC3 player flags?)
    NPCBYTECODE = 131          # Compiled NPC script
    UNKNOWN132 = 132           # Unknown (unique, unhandled by 6.037)
    UNKNOWN133 = 133           # Unknown (unique, unhandled by 6.037)
    GANISCRIPT = 134           # Gani script
    NPCWEAPONSCRIPT = 140      # Weapon script
    NPCDEL2 = 150              # NPC deleted (with level)
    HIDENPCS = 151             # Hide NPCs
    SAY2 = 153                 # Say/sign text
    FREEZEPLAYER2 = 154        # Freeze player
    UNFREEZEPLAYER = 155       # Unfreeze player
    SETACTIVELEVEL = 156       # Set active level
    NC_NPCATTRIBUTES = 157     # NPC attributes
    NC_NPCADD = 158            # NPC add
    NC_NPCDELETE = 159         # NPC delete
    NC_NPCSCRIPT = 160         # NPC script
    NC_NPCFLAGS = 161          # NPC flags
    NC_CLASSGET = 162          # Class get
    NC_CLASSADD = 163          # Class add
    NC_LEVELDUMP = 164         # Level dump
    MOVE = 165                 # Move (unhandled by 6.037)
    UNKNOWN166 = 166           # Unknown
    NC_WEAPONLISTGET = 167     # Weapon list get
    UNKNOWN168 = 168           # Unknown (blank from login server)
    UNKNOWN169 = 169           # Unknown (pointer value?, unhandled by 6.037)
    GHOSTMODE = 170            # Ghost mode
    BIGMAP = 171               # Big map (unhandled by 6.037)
    MINIMAP = 172              # Mini map
    GHOSTTEXT = 173            # Ghost mode text
    GHOSTICON = 174            # Ghost mode icon
    SHOOT = 175                # Shoot (unhandled by 6.037)
    FULLSTOP = 176             # Full stop (hides HUD, stops input)
    FULLSTOP2 = 177            # Full stop 2 (unhandled by 5.07+)
    SERVERWARP = 178           # Server warp
    RPGWINDOW = 179            # RPG window
    STATUSLIST = 180           # Status list
    UNKNOWN181 = 181           # Unknown (unique, unhandled by 6.037)
    LISTPROCESSES = 182        # List processes
    UNKNOWN183 = 183           # Unknown
    UNKNOWN184 = 184           # Unknown (screenshots?, unhandled by 6.037)
    UNKNOWN185 = 185           # Unknown
    UNKNOWN186 = 186           # Unknown
    UPDATEPACKAGEISUPDATED = 187  # Update package is updated
    NC_CLASSDELETE = 188       # Class delete
    MOVE2 = 189                # Move 2
    UNKNOWN190 = 190           # Unknown (triggers IRC login, etc.)
    SHOOT2 = 191               # Shoot 2
    NC_WEAPONGET = 192         # Weapon get
    UNKNOWN193 = 193           # Unknown (5-byte int?, unhandled by 6.037)
    CLEARWEAPONS = 194         # Clear weapons
    UNKNOWN195 = 195           # Unknown (ganis?)
    UNKNOWN197 = 197           # Unknown (NPC registration, offline cache)
    UNKNOWN198 = 198           # Unknown


# =============================================================================
# PLPROP - Player Property IDs
# =============================================================================
class PLPROP(IntEnum):
    """Player property IDs"""
    NICKNAME = 0               # String: Nickname
    MAXPOWER = 1               # 1 byte: Max hearts
    CURPOWER = 2               # 1 byte: Current hearts (x2)
    RUPEESCOUNT = 3            # 3 bytes: Rupees (gInt3)
    ARROWSCOUNT = 4            # 1 byte: Arrows
    BOMBSCOUNT = 5             # 1 byte: Bombs
    GLOVEPOWER = 6             # 1 byte: Glove level
    BOMBPOWER = 7              # 1 byte: Bomb power
    SWORDPOWER = 8             # 1 byte + optional image string
    SHIELDPOWER = 9            # 1 byte + optional image string
    GANI = 10                  # String: Animation (BOWGIF in pre-2.x)
    HEADIMAGE = 11             # String: Head image
    CURCHAT = 12               # String: Current chat (above head)
    COLORS = 13                # 5 bytes: Colors
    ID = 14                    # 2 bytes: Player ID
    X = 15                     # 1 byte: X position (x*2 half-tiles)
    Y = 16                     # 1 byte: Y position (y*2 half-tiles)
    SPRITE = 17                # 1 byte: Sprite/direction
    DIRECTION = 17             # Alias for SPRITE (direction in lower 2 bits)
    STATUS = 18                # 1 byte: Status flags
    CARRYSPRITE = 19           # 1 byte: Carried sprite
    CURLEVEL = 20              # String: Current level
    HORSEGIF = 21              # String: Horse image
    HORSEBUSHES = 22           # 1 byte: Horse bushes
    EFFECTCOLORS = 23          # Effect colors
    CARRYNPC = 24              # Carried NPC ID
    APCOUNTER = 25             # AP counter
    MAGICPOINTS = 26           # 1 byte: Magic points
    KILLSCOUNT = 27            # 3 bytes: Kills
    DEATHSCOUNT = 28           # 3 bytes: Deaths
    ONLINESECS = 29            # 3 bytes: Online seconds
    IPADDR = 30                # IP address
    UDPPORT = 31               # UDP port
    ALIGNMENT = 32             # 1 byte: Alignment (AP)
    ADDITFLAGS = 33            # Additional flags
    ACCOUNTNAME = 34           # String: Account name
    BODYIMAGE = 35             # String: Body image
    RATING = 36                # 4 bytes: Rating
    GATTRIB1 = 37              # String: Custom attribute 1
    GATTRIB2 = 38              # String: Custom attribute 2
    GATTRIB3 = 39              # String: Custom attribute 3
    GATTRIB4 = 40              # String: Custom attribute 4
    GATTRIB5 = 41              # String: Custom attribute 5
    ATTACHNPC = 42             # Attached NPC ID
    GMAPLEVELX = 43            # GMAP level X
    GMAPLEVELY = 44            # GMAP level Y
    Z = 45                     # Z position
    GATTRIB6 = 46              # String: Custom attribute 6
    GATTRIB7 = 47              # String: Custom attribute 7
    GATTRIB8 = 48              # String: Custom attribute 8
    GATTRIB9 = 49              # String: Custom attribute 9
    JOINLEAVELVL = 50          # Join/leave level
    PCONNECTED = 51            # Connected
    PLANGUAGE = 52             # Language
    PSTATUSMSG = 53            # Status message
    GATTRIB10 = 54             # String: Custom attribute 10
    GATTRIB11 = 55             # String: Custom attribute 11
    GATTRIB12 = 56             # String: Custom attribute 12
    GATTRIB13 = 57             # String: Custom attribute 13
    GATTRIB14 = 58             # String: Custom attribute 14
    GATTRIB15 = 59             # String: Custom attribute 15
    GATTRIB16 = 60             # String: Custom attribute 16
    GATTRIB17 = 61             # String: Custom attribute 17
    GATTRIB18 = 62             # String: Custom attribute 18
    GATTRIB19 = 63             # String: Custom attribute 19
    GATTRIB20 = 64             # String: Custom attribute 20
    GATTRIB21 = 65             # String: Custom attribute 21
    GATTRIB22 = 66             # String: Custom attribute 22
    GATTRIB23 = 67             # String: Custom attribute 23
    GATTRIB24 = 68             # String: Custom attribute 24
    GATTRIB25 = 69             # String: Custom attribute 25
    GATTRIB26 = 70             # String: Custom attribute 26
    GATTRIB27 = 71             # String: Custom attribute 27
    GATTRIB28 = 72             # String: Custom attribute 28
    GATTRIB29 = 73             # String: Custom attribute 29
    GATTRIB30 = 74             # String: Custom attribute 30
    OSTYPE = 75                # String: OS type (2.19+)
    TEXTCODEPAGE = 76          # 3 bytes: Text codepage (2.19+)
    UNKNOWN77 = 77             # Unknown
    X2 = 78                    # 2 bytes: X position (pixels/16)
    Y2 = 79                    # 2 bytes: Y position (pixels/16)
    Z2 = 80                    # 2 bytes: Z position
    UNKNOWN81 = 81             # Unknown (playerlist placement flag)
    COMMUNITYNAME = 82         # String: Community name (Reborn v5)


# Total property count
PLPROP_COUNT = 83


# =============================================================================
# NPCPROP - NPC Property IDs
# =============================================================================
class NPCPROP(IntEnum):
    """NPC property IDs"""
    IMAGE = 0                  # String: NPC image
    SCRIPT = 1                 # String (gShort length): Script
    X = 2                      # 1 byte: X position (x*2)
    Y = 3                      # 1 byte: Y position (y*2)
    POWER = 4                  # 1 byte: Power/health
    RUPEES = 5                 # 3 bytes: Rupees
    ARROWS = 6                 # 1 byte: Arrows/darts
    BOMBS = 7                  # 1 byte: Bombs
    GLOVEPOWER = 8             # 1 byte: Glove power
    BOMBPOWER = 9              # 1 byte: Bomb power
    SWORDIMAGE = 10            # String: Sword image
    SHIELDIMAGE = 11           # String: Shield image
    GANI = 12                  # String: Animation (BOWGIF in pre-2.x)
    VISFLAGS = 13              # 1 byte: Visibility flags
    BLOCKFLAGS = 14            # 1 byte: Block flags
    MESSAGE = 15               # String: Message
    HURTDXDY = 16              # 2 bytes: Hurt dx/dy
    ID = 17                    # 3 bytes: NPC ID
    SPRITE = 18                # 1 byte: Sprite
    COLORS = 19                # 5 bytes: Colors
    NICKNAME = 20              # String: Nickname
    HORSEIMAGE = 21            # String: Horse image
    HEADIMAGE = 22             # String: Head image
    SAVE0 = 23                 # 1 byte: Save slot 0
    SAVE1 = 24                 # 1 byte: Save slot 1
    SAVE2 = 25                 # 1 byte: Save slot 2
    SAVE3 = 26                 # 1 byte: Save slot 3
    SAVE4 = 27                 # 1 byte: Save slot 4
    SAVE5 = 28                 # 1 byte: Save slot 5
    SAVE6 = 29                 # 1 byte: Save slot 6
    SAVE7 = 30                 # 1 byte: Save slot 7
    SAVE8 = 31                 # 1 byte: Save slot 8
    SAVE9 = 32                 # 1 byte: Save slot 9
    ALIGNMENT = 33             # 1 byte: Alignment
    IMAGEPART = 34             # Image part (offset, size)
    BODYIMAGE = 35             # String: Body image
    GATTRIB1 = 36              # String: Custom attribute 1
    GATTRIB2 = 37              # String: Custom attribute 2
    GATTRIB3 = 38              # String: Custom attribute 3
    GATTRIB4 = 39              # String: Custom attribute 4
    GATTRIB5 = 40              # String: Custom attribute 5
    GMAPLEVELX = 41            # GMAP level X
    GMAPLEVELY = 42            # GMAP level Y
    UNKNOWN43 = 43             # Unknown
    GATTRIB6 = 44              # String: Custom attribute 6
    GATTRIB7 = 45              # String: Custom attribute 7
    GATTRIB8 = 46              # String: Custom attribute 8
    GATTRIB9 = 47              # String: Custom attribute 9
    UNKNOWN48 = 48             # Unknown
    SCRIPTER = 49              # Scripter name
    NAME = 50                  # NPC name
    TYPE = 51                  # NPC type
    CURLEVEL = 52              # Current level
    GATTRIB10 = 53             # String: Custom attribute 10
    GATTRIB11 = 54             # String: Custom attribute 11
    GATTRIB12 = 55             # String: Custom attribute 12
    GATTRIB13 = 56             # String: Custom attribute 13
    GATTRIB14 = 57             # String: Custom attribute 14
    GATTRIB15 = 58             # String: Custom attribute 15
    GATTRIB16 = 59             # String: Custom attribute 16
    GATTRIB17 = 60             # String: Custom attribute 17
    GATTRIB18 = 61             # String: Custom attribute 18
    GATTRIB19 = 62             # String: Custom attribute 19
    GATTRIB20 = 63             # String: Custom attribute 20
    GATTRIB21 = 64             # String: Custom attribute 21
    GATTRIB22 = 65             # String: Custom attribute 22
    GATTRIB23 = 66             # String: Custom attribute 23
    GATTRIB24 = 67             # String: Custom attribute 24
    GATTRIB25 = 68             # String: Custom attribute 25
    GATTRIB26 = 69             # String: Custom attribute 26
    GATTRIB27 = 70             # String: Custom attribute 27
    GATTRIB28 = 71             # String: Custom attribute 28
    GATTRIB29 = 72             # String: Custom attribute 29
    GATTRIB30 = 73             # String: Custom attribute 30
    CLASS = 74                 # NPC class (NPC-Server)
    X2 = 75                    # 2 bytes: X position (pixels)
    Y2 = 76                    # 2 bytes: Y position (pixels)


# Total NPC property count
NPCPROP_COUNT = 77


# =============================================================================
# BDPROP - Baddy Property IDs
# =============================================================================
class BDPROP(IntEnum):
    """Baddy property IDs"""
    ID = 0                     # Baddy ID
    X = 1                      # X position
    Y = 2                      # Y position
    TYPE = 3                   # Baddy type
    POWERIMAGE = 4             # Power and image
    MODE = 5                   # Baddy mode
    ANI = 6                    # Animation
    DIR = 7                    # Direction
    VERSESIGHT = 8             # Verse on sight
    VERSEHURT = 9              # Verse on hurt
    VERSEATTACK = 10           # Verse on attack


BDPROP_COUNT = 11


# =============================================================================
# BDMODE - Baddy Mode IDs
# =============================================================================
class BDMODE(IntEnum):
    """Baddy mode IDs"""
    WALK = 0                   # Walking
    LOOK = 1                   # Looking
    HUNT = 2                   # Hunting player
    HURT = 3                   # Being hurt
    BUMPED = 4                 # Bumped
    DIE = 5                    # Dying
    SWAMPSHOT = 6              # Swamp shot (type 4)
    HAREJUMP = 7               # Hare jump
    OCTOSHOT = 8               # Octopus shot
    DEAD = 9                   # Dead


BDMODE_COUNT = 10


# =============================================================================
# LevelItemType - Ground Item Types
# =============================================================================
class LevelItemType(IntEnum):
    """Level item types"""
    INVALID = -1               # Invalid item

    GREENRUPEE = 0             # Green rupee (1)
    BLUERUPEE = 1              # Blue rupee (5)
    REDRUPEE = 2               # Red rupee (30)
    BOMBS = 3                  # Bombs
    DARTS = 4                  # Darts/arrows
    HEART = 5                  # Heart
    GLOVE1 = 6                 # Glove level 1
    BOW = 7                    # Bow
    BOMB = 8                   # Bomb weapon
    SHIELD = 9                 # Shield
    SWORD = 10                 # Sword
    FULLHEART = 11             # Full heart
    SUPERBOMB = 12             # Super bomb
    BATTLEAXE = 13             # Battle axe
    GOLDENSWORD = 14           # Golden sword
    MIRRORSHIELD = 15          # Mirror shield
    GLOVE2 = 16                # Glove level 2
    LIZARDSHIELD = 17          # Lizard shield
    LIZARDSWORD = 18           # Lizard sword
    GOLDRUPEE = 19             # Gold rupee (100)
    FIREBALL = 20              # Fireball
    FIREBLAST = 21             # Fire blast
    NUKESHOT = 22              # Nuke shot
    JOLTBOMB = 23              # Jolt bomb
    SPINATTACK = 24            # Spin attack


# =============================================================================
# PLTYPE - Player/Connection Types
# =============================================================================
class PLTYPE(IntFlag):
    """Player/connection types"""
    AWAIT = -1                 # Awaiting type
    CLIENT = 1 << 0            # Game client
    RC = 1 << 1                # Remote control
    NPCSERVER = 1 << 2         # NPC server
    NC = 1 << 3                # NPC control
    CLIENT2 = 1 << 4           # Client v2
    CLIENT3 = 1 << 5           # Client v3
    RC2 = 1 << 6               # RC v2
    EXTERNAL = 1 << 7          # External (IRC)
    WEB = 1 << 8               # Web client

    # Composite types
    ANYCLIENT = CLIENT | CLIENT2 | CLIENT3 | WEB
    ANYRC = RC | RC2
    ANYNC = NC
    ANYCONTROL = ANYRC | ANYNC
    ANYPLAYER = ANYCLIENT | ANYRC
    NONITERABLE = NPCSERVER | ANYNC | EXTERNAL


# =============================================================================
# PLSTATUS - Player Status Flags
# =============================================================================
class PLSTATUS(IntFlag):
    """Player status flags"""
    PAUSED = 0x01              # Paused
    HIDDEN = 0x02              # Hidden/invisible
    MALE = 0x04                # Male gender
    DEAD = 0x08                # Dead
    ALLOWWEAPONS = 0x10        # Weapons allowed
    HIDESWORD = 0x20           # Hide sword
    HASSPIN = 0x40             # Has spin attack


# =============================================================================
# PLFLAG - Player Flags
# =============================================================================
class PLFLAG(IntFlag):
    """Player flags"""
    NOMASSMESSAGE = 0x01       # No mass messages
    ONLYSHOWONELEVEL = 0x02    # Only show one level
    NOTOALL = 0x04             # No toall messages
    VOICEDISABLED = 0x08       # Voice disabled


# =============================================================================
# PLPERM - Player Permissions (Admin Rights)
# =============================================================================
class PLPERM(IntFlag):
    """Player permissions (admin rights)"""
    WARPTO = 0x00001           # Warp to level
    WARPTOPLAYER = 0x00002     # Warp to player
    SUMMON = 0x00004           # Summon player
    UPDATELEVEL = 0x00008      # Update level
    DISCONNECT = 0x00010       # Disconnect player
    VIEWATTRIBUTES = 0x00020   # View player attributes
    SETATTRIBUTES = 0x00040    # Set player attributes
    SETSELFATTRIBUTES = 0x00080  # Set own attributes
    RESETATTRIBUTES = 0x00100  # Reset player attributes
    ADMINMSG = 0x00200         # Send admin messages
    SETRIGHTS = 0x00400        # Set player rights
    BAN = 0x00800              # Ban player
    SETCOMMENTS = 0x01000      # Set player comments
    INVISIBLE = 0x02000        # Go invisible
    SETSERVEROPTIONS = 0x04000 # Set server options
    SETSERVERFLAGS = 0x08000   # Set server flags
    SETFOLDERCONFIG = 0x10000  # Set folder config
    SETACCOUNT = 0x20000       # Set account info
    FILEBROWSER = 0x40000      # Use file browser
    SERVERWARP = 0x80000       # Server warp


# =============================================================================
# NPCVISFLAG - NPC Visibility Flags
# =============================================================================
class NPCVISFLAG(IntFlag):
    """NPC visibility flags"""
    VISIBLE = 0x01             # NPC is visible
    DRAWOVERPLAYER = 0x02      # Draw over player
    DRAWUNDERPLAYER = 0x04     # Draw under player


# =============================================================================
# NPCBLOCKFLAG - NPC Blocking Flags
# =============================================================================
class NPCBLOCKFLAG(IntFlag):
    """NPC blocking flags"""
    BLOCK = 0x00               # NPC blocks movement
    NOBLOCK = 0x01             # NPC does not block


# =============================================================================
# SVO - Server -> ListServer Packet IDs (Server Output)
# =============================================================================
class SVO(IntEnum):
    """Server -> ListServer packet IDs (Server Output)"""
    SETNAME = 0                # Set server name (deprecated)
    SETDESC = 1                # Set description (deprecated)
    SETLANG = 2                # Set language (deprecated)
    SETVERS = 3                # Set version (deprecated)
    SETURL = 4                 # Set URL (deprecated)
    SETIP = 5                  # Set IP (deprecated)
    SETPORT = 6                # Set port (deprecated)
    SETPLYR = 7                # Clear players (start of player list)
    VERIACC = 8                # Verify account (deprecated)
    VERIGUILD = 9              # Verify guild/nickname
    GETFILE = 10               # Get file (deprecated)
    NICKNAME = 11              # Set nickname
    GETPROF = 12               # Get profile
    SETPROF = 13               # Set profile
    PLYRADD = 14               # Add player to list
    PLYRREM = 15               # Remove player from list
    PING = 16                  # Ping/keepalive
    VERIACC2 = 17              # Verify account (current)
    SETLOCALIP = 18            # Set local IP
    GETFILE2 = 19              # Get file v2 (deprecated)
    UPDATEFILE = 20            # Update file
    GETFILE3 = 21              # Get file v3
    NEWSERVER = 22             # Register new server (full info)
    SERVERHQPASS = 23          # Server HQ password
    SERVERHQLEVEL = 24         # Server HQ level
    SERVERINFO = 25            # Server info
    REQUESTLIST = 26           # Request list
    REQUESTSVRINFO = 27        # Request server info
    REQUESTBUDDIES = 28        # Request buddies
    PMPLAYER = 29              # PM player across servers
    REGISTERV3 = 30            # Register v3 (modern handshake)
    SENDTEXT = 31              # Send text/command
    PACKETCOUNT = 32           # Packet count


# =============================================================================
# SVI - ListServer -> Server Packet IDs (Server Input)
# =============================================================================
class SVI(IntEnum):
    """ListServer -> Server packet IDs (Server Input)"""
    VERIACC = 0                # Verify account response (deprecated)
    VERIGUILD = 1              # Verify guild response
    FILESTART = 2              # File transfer start (deprecated)
    FILEEND = 3                # File transfer end (deprecated)
    FILEDATA = 4               # File transfer data (deprecated)
    VERSIONOLD = 5             # Server version is old
    VERSIONCURRENT = 6         # Server version is current
    PROFILE = 7                # Profile response
    ERRMSG = 8                 # Error message
    NULL4 = 9                  # Null packet
    NULL5 = 10                 # Null packet
    VERIACC2 = 11              # Verify account response (current)
    FILESTART2 = 12            # File transfer start v2 (deprecated)
    FILEDATA2 = 13             # File transfer data v2 (deprecated)
    FILEEND2 = 14              # File transfer end v2 (deprecated)
    FILESTART3 = 15            # File transfer start v3
    FILEDATA3 = 16             # File transfer data v3
    FILEEND3 = 17              # File transfer end v3
    SERVERINFO = 18            # Server info response
    REQUESTTEXT = 19           # Request text from server
    SENDTEXT = 20              # Send text to server
    PMPLAYER = 29              # PM player response
    ASSIGNPCID = 30            # Assign PC ID (device ID)
    PING = 99                  # Ping/keepalive
    RAWDATA = 100              # Raw data
    PACKETCOUNT = 101          # Packet count

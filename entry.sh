#!/bin/bash
chmod -R 777 /home/steam/
/home/steam/steamcmd.sh +force_install_dir /home/steam/steamworks_sdk +login anonymous +@sSteamCmdForcePlatformType windows +app_update 1007 +quit
/home/steam/steamcmd.sh @sSteamCmdForcePlatformType windows +force_install_dir /home/steam/sw +login anonymous +app_update 1247090  +quit
cp /home/steam/steamworks_sdk/*64.dll /home/steam/sw
winecfg
xvfb-run wine /home/steam/sw/server64.exe

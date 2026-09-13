## Stormworks dedicated server with built-in web control panel.
## Base image ships a current, pre-updated SteamCMD (the old public tarball's
## self-updating bootstrap fails on many networks) and runs as non-root "steam".
FROM cm2network/steamcmd:latest

USER root
ENV DEBIAN_FRONTEND=noninteractive

## wine (Debian repo build - enough for the 64-bit dedicated server),
## Xvfb for headless wine, qrencode for the Steam QR login flow
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        wine64 xvfb qrencode python3 && \
    rm -rf /var/lib/apt/lists/*

RUN mkdir -p /home/steam/sw /home/steam/steamworks_sdk && \
    chown -R steam:steam /home/steam/sw /home/steam/steamworks_sdk

COPY panel.py /opt/panel.py
COPY entry.sh /opt/entry.sh
RUN chmod +x /opt/entry.sh

## k8s PVCs mount empty over /home/steam/steamcmd (unlike docker named
## volumes, which seed from the image) - keep a fallback copy to re-seed from
RUN cp -a /home/steam/steamcmd /opt/steamcmd-fallback && \
    chown -R steam:steam /opt/steamcmd-fallback

USER steam
ENV HOME=/home/steam \
    WINEPREFIX=/home/steam/wine-data/prefix \
    PANEL_PORT=8080

WORKDIR /home/steam

## game ports + control panel
EXPOSE 25564-25566/tcp 25564-25566/udp 8080/tcp

ENTRYPOINT ["/opt/entry.sh"]

FROM ubuntu:24.04

##set up base
WORKDIR /home/steam/sw
RUN export DEBIAN_FRONTEND=noninteractive
RUN apt update && apt upgrade -y
RUN apt-get update && \
    apt-get install -yq tzdata && \
    ln -fs /usr/share/zoneinfo/America/Boise /etc/localtime && \
    dpkg-reconfigure -f noninteractive tzdata
RUN apt install software-properties-common -y
RUN dpkg --add-architecture i386
RUN apt update && apt upgrade -y

## install base dependencies for headless xorg
RUN apt install -y lib32gcc-s1 curl wget xvfb apt-utils rsync

## add wine repo and install wine-staging
RUN wget -nc -O /etc/apt/keyrings/winehq-archive.key https://dl.winehq.org/wine-builds/winehq.key
RUN wget -NP /etc/apt/sources.list.d/ https://dl.winehq.org/wine-builds/ubuntu/dists/noble/winehq-noble.sources
RUN apt update && apt install --install-recommends wine64 -y

COPY entry.sh /opt/
RUN chmod +x /opt/entry.sh

ENTRYPOINT /opt/entry.sh
#ENTRYPOINT [ "bash" ]

EXPOSE 25564/tcp
EXPOSE 25564/udp
EXPOSE 25565/tcp
EXPOSE 25565/udp
EXPOSE 25566/tcp
EXPOSE 25566/udp

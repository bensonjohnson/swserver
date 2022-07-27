FROM ubuntu:latest
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
RUN apt install -y lib32gcc-s1 curl wget xvfb
RUN wget -nc -O /usr/share/keyrings/winehq-archive.key https://dl.winehq.org/wine-builds/winehq.key
RUN wget -nc -P /etc/apt/sources.list.d/ https://dl.winehq.org/wine-builds/ubuntu/dists/jammy/winehq-jammy.sources
RUN apt update && apt install --install-recommends wine-stable mono-complete winetricks -y
RUN adduser --disabled-password --home /home/steam steam
RUN cd /home/steam && curl -sqL "https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz" | tar zxvf -
RUN chown -R steam:steam /home/steam/
RUN chmod +x /home/steam/steamcmd.sh
RUN chmod -R 777 /home/steam/
COPY entry.sh /home/

ENTRYPOINT /home/entry.sh

EXPOSE 25564/tcp
EXPOSE 25565/tcp
EXPOSE 25566/tcp
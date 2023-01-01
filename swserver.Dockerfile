FROM ubuntu:22.04
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
RUN apt install -y lib32gcc-s1 curl wget xvfb apt-utils
RUN wget -nc -O /etc/apt/keyrings/winehq-archive.key https://dl.winehq.org/wine-builds/winehq.key
RUN wget -nc -P /etc/apt/sources.list.d/ https://dl.winehq.org/wine-builds/ubuntu/dists/jammy/winehq-jammy.sources
RUN apt update && apt install --install-recommends winehq-staging -y
RUN adduser --disabled-password --home /home/steam steam
RUN cd /home/steam && curl -sqL "https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz" | tar zxvf -
RUN chown -R steam:steam /home/steam/
RUN chmod -R 755 /opt/
RUN chmod +x /home/steam/steamcmd.sh
RUN chmod -R 755 /home/
COPY entry.sh /opt/
RUN chmod +x /opt/entry.sh

ENTRYPOINT /opt/entry.sh

EXPOSE 25564/tcp
EXPOSE 25564/udp
EXPOSE 25565/tcp
EXPOSE 25565/udp
EXPOSE 25566/tcp
EXPOSE 25566/udp

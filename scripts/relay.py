# Relay messages from channels on one server to channels on another

TO_HOST = ""

if line.channel and connection.host != TO_HOST:
    for c in connection.bot.connections.values():
        if c.host == TO_HOST:
            for channel in c.dchannels:
                c.msg(channel, str(line))

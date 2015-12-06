# Relay messages from a server to a channel

TO_HOST = ""

if line.channel and connection.host != TO_HOST:
    for c in connection.bot.connections.values():
        if c.host == TO_HOST:
            for channel in c.dchannels:
                c.msg(channel, str(line))

PERMITTED_USERS = []

if line.args and connection.nick in line.args[0].split()[0]:
    if line.nick in PERMITTED_USERS:
        args = line.args[0].split()
        args.pop(0)
        if args[0] == "run":
            args.pop(0)
            import subprocess
            output = subprocess.Popen(args, stdout=subprocess.PIPE).communicate()[0]
            for out in output.split("\n"):
                if out:
                    connection.msg(line.channel, out)

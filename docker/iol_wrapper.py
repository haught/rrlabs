#!/usr/bin/env python3

import array, atexit, fcntl, getopt, os, select, signal, socket, struct, subprocess, sys, time
from functions import *

def exit_handler():
    print("EXITING")
    print(globals())
    print(type(from_iol))
    if "from_iol" in globals():
        print("IOL")
        from_iol.close()
    print(type(from_switcherd))
    if "from_switcherd" in globals():
        print("SWITCHERD")
        from_switcherd.close()
    print(type(from_tun))
    if "from_tun" in globals():
        print("TUN")
        from_tun.close()
    print(netmap)
    if "netmap" in globals():
        print("NETMAP")
        os.unlink(netmap)
    if time.time() - alive < MIN_TIME:
        sys.stderr.write("ERROR: IOL process died unexpectedly\n")
        print(console_history)
        if "console_history" in globals():
            print(console_history)
    #else:
    #   sys.exit(0)

def exit_gracefully(signum, frame):
    # restore the original signal handler as otherwise evil things will happen
    # in raw_input when CTRL+C is pressed, and our signal handler is not re-entrant
    signal.signal(signal.SIGINT, original_sigint)

    if signum == 2:
        # CTRL+C
        sys.exit(3)

    # restore the exit gracefully handler here    
    signal.signal(signal.SIGINT, exit_gracefully)

def usage():
    print("Usage: {} [OPTIONS]".format(sys.argv[0]))
    print("  -f IOL")
    print("     IOL binary executable")
    print("  -g hostname")
    print("     switcherd hostname or IP address")
    print("  -i iol_id")
    print("     IOL device ID")
    print("  -l label")
    print("     Node label")

def main():
    global alive, console_history, from_iol, from_switcherd, from_tun, netmap

    # Reading options
    try:
        opts, args = getopt.getopt(sys.argv[1:], "f:g:i:l:n:")
    except getopt.GetoptError as err:
        sys.stderr.write("ERROR: {}\n".format(err))
        usage()
        sys.exit(1)

    # Parsing options
    for o, a in opts:
        if o == "-f":
            iol = a
        elif o == "-g":
            switcherd = a
        elif o == "-i":
            iol_id = int(a)
        elif o == "-l":
            label = int(a)
        else:
            assert False, "unhandled option"

    # Checking options
    if "iol" not in locals():
        sys.stderr.write("ERROR: missing IOL binary executable\n")
        usage()
        sys.exit(1)
    if not os.path.isfile(iol):
        sys.stderr.write("ERROR: cannot find IOL binary executable\n")
        usage()
        sys.exit(1)
    if "iol_id" not in locals():
        sys.stderr.write("ERROR: missing iol_id\n")
        usage()
        sys.exit(1)
    if iol_id < 1 or iol_id > 1024:
        sys.stderr.write("ERROR: iol_id must be between 1 and 1024\n")
        usage()
        sys.exit(1)
    if "switcherd" not in locals():
        sys.stderr.write("ERROR: missing switcherid\n")
        usage()
        sys.exit(1)
    if "label" not in locals():
        sys.stderr.write("ERROR: missing label\n")
        usage()
        sys.exit(1)

    # Setting parameters
    if iol_id == 1024:
        wrapper_id = 1
    else:
        wrapper_id = iol_id + 1

    read_fsocket = "/tmp/netio0/{}".format(wrapper_id)
    write_fsocket = "/tmp/netio0/{}".format(iol_id)

    # Writing NETMAP
    netmap = os.path.basename(iol)
    try:
        os.unlink(netmap)
    except OSError:
        if os.path.exists(netmap):
            sys.stderr.write("ERROR: cannot delete existent NETMAP")
            sys.exit(1)
    netmap_fd = open(netmap, 'w')
    for i in range(0, 63):
        netmap_fd.write("{}:{} {}:{}\n".format(iol_id, i, wrapper_id, i))
    netmap_fd.close()

    # Preparing socket (IOL -> wrapper)
    try:
        os.unlink(read_fsocket)
    except OSError:
        if os.path.exists(read_fsocket):
            sys.stderr.write("ERROR: cannot delete existent socket")
            sys.exit(1)
    from_iol = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    from_iol.bind(read_fsocket)

    # Preparing socket (wrapper -> IOL)
    if not os.path.exists(read_fsocket):
        sys.stderr.write("ERROR: IOL node not running\n")

    # Preparing socket (switcherd -> wrapper)
    from_switcherd = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    from_switcherd.bind(('', UDP_PORT))

    # Preparing socket (wrapper -> switcherd)
    to_switcherd = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    # Preparing tap
    from_tun = open("/dev/net/tun", "r+b", buffering = 0)
    ifr = struct.pack('16sH', b"veth0", IFF_TAP | IFF_NO_PI)
    fcntl.ioctl(from_tun, TUNSETIFF, ifr)
    fcntl.ioctl(from_tun, TUNSETNOCSUM, 1)

    # Starting IOL
    iol = subprocess.Popen([iol, ""], stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    inputs = [ from_iol, from_switcherd, from_tun, iol.stdout.fileno(), iol.stderr.fileno() ]
    outputs = [ ]

    console_history = bytearray()
    print(globals())

    while inputs:
        if DEBUG: print("DEBUG: waiting for data")

        if iol.poll() != None:
            if DEBUG: print("ERROR: IOL process died")
            # Grab all output before exiting
            console_history += iol.stdout.read()
            console_history += iol.stderr.read()
            sys.exit(2)

        readable, writable, exceptional = select.select(inputs, outputs, inputs)

        if "to_iol" not in locals():
            try:
                to_iol = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                to_iol.connect(write_fsocket)
            except Exception as err:
                sys.stderr.write("ERROR: cannot connect to IOL socket\n")
                del(to_iol)
                pass

        for s in readable:
            if s is from_iol:
                if DEBUG: print("DEBUG: data from IOL")
                iol_datagram = from_iol.recv(IOL_BUFFER)
                if not iol_datagram:
                    sys.stderr.write("ERROR: cannot receive data from IOL node\n")
                    break
                else:
                    src_id, src_if, dst_id, dst_if, padding, payload = decodeIOLPacket(iol_datagram)
                    if src_id == MGMT_ID:
                        try:
                            os.write(from_tun.fileno(), payload)
                        except Exception as err:
                            sys.stderr.write("ERROR: cannot send data to MGMT\n")
                            sys.exit(2)
                    else:
                        try:
                            to_switcherd.sendto(encodeUDPPacket(label, src_if, payload), (switcherd, UDP_PORT))
                        except Exception as err:
                            sys.stderr.write("ERROR: cannot send data to switcherd\n")
                            sys.exit(2)
            elif s is from_switcherd:
                if DEBUG: print("DEBUG: data from UDP")
                udp_datagram, src_addr = from_switcherd.recvfrom(UDP_BUFFER)
                if not udp_datagram:
                    sys.stderr.write("ERROR: cannot receive data from switcherd\n")
                    break
                else:
                    label, iface, payload = decodeUDPPacket(udp_datagram)
                    if "to_iol" in locals():
                        try:
                            to_iol.send(encodeIOLPacket(wrapper_id, iol_id, iface, payload))
                        except Exception as err:
                            sys.stderr.write("ERROR: cannot send data to IOL node\n")
                            sys.exit(2)
                    else:
                        sys.stderr.write("ERROR: cannot connect to IOL socket, packet dropped\n")
            elif s is from_tun:
                if DEBUG: print("DEBUG: data from MGMT")
                tap_datagram = array.array('B', os.read(from_tun.fileno(), TAP_BUFFER))
                if "to_iol" in locals():
                    try:
                        to_iol.send(encodeIOLPacket(wrapper_id, iol_id, MGMT_ID, tap_datagram))
                    except Exception as err:
                        sys.stderr.write("ERROR: cannot send data to IOL MGMT\n")
                        sys.exit(2)
                else:
                    sys.stderr.write("ERROR: cannot connect to IOL socket, packet dropped\n")
            elif s is iol.stdout.fileno():
                read = iol.stdout.read(1)
                if time.time() - alive < MIN_TIME:
                    console_history += read
            elif s is iol.stderr.fileno():
                read = iol.stderr.read(1)
                if time.time() - alive < MIN_TIME:
                    console_history += read
            else:
                sys.stderr.write("ERROR: unknown source from select\n")

if __name__ == "__main__":
    alive = time.time()
    atexit.register(exit_handler)
    original_sigint = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, exit_gracefully)
    main()
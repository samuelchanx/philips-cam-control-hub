import socket, struct, threading, time, sys
DUR = int(sys.argv[1]) if len(sys.argv) > 1 else 90
stop = time.time() + DUR
vf = open("/tmp/philips_apk/p2p/ch1_video.bin", "wb")
tot = [0, 0]

def rd(port, name):
    while time.time() < stop:
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=3)
            s.settimeout(3)
            print(f"[+] {name} up", flush=True)
            while time.time() < stop:
                try:
                    d = s.recv(262144)
                    if not d: print(f"[!] {name} eof"); break
                    if port == 9100:
                        tot[0] += len(d)
                        if len(d) >= 24:
                            m, ln, cmd = struct.unpack("<III", d[:12])
                            if m == 0x76768888:
                                print(f"[<] {name} cmd={cmd:#x} len={ln} {d[24:32].hex()}", flush=True)
                    else:
                        tot[1] += len(d)
                        vf.write(d); vf.flush()
                except socket.timeout: pass
        except Exception as e:
            print(f"[!] {name}: {e}", flush=True); time.sleep(1)

t0 = threading.Thread(target=rd, args=(9100, "ch0"))
t1 = threading.Thread(target=rd, args=(9101, "ch1"))
t0.start(); t1.start(); t0.join(); t1.join()
vf.close()
print(f"[=] ch0={tot[0]}B ch1={tot[1]}B")

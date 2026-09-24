/*
 * wiimote-hci: the few raw Bluetooth HCI operations WiiBridge needs,
 * so the app doesn't depend on hcitool / hciconfig / btmon -- Fedora-based
 * distros no longer ship them, and immutable ones (Bazzite, Silverblue,
 * SteamOS) can't have capabilities set on system binaries anyway. Needs
 * CAP_NET_RAW (and CAP_NET_ADMIN for "up"), granted once to this file.
 *
 *   wiimote-hci list                    one line per adapter: hciN ADDR UP|DOWN
 *   wiimote-hci up hciN                 bring an adapter up
 *   wiimote-hci scan hciN SECS LAPHEX   inquiry using access code LAPHEX
 *                                       (9e8b00 = limited, the one Wii remotes
 *                                       answer; 9e8b33 = general); prints
 *                                       "ADDR 0xCLASS" for each device heard
 *
 * The inquiry is sent as a raw HCI command rather than through the kernel's
 * inquiry ioctl or its "limited discovery" because on some kernels (seen on
 * 7.0) both of those send a general inquiry whatever is asked for, which Wii
 * remotes never answer.
 *
 * Structures are declared here rather than taken from libbluetooth headers,
 * so building needs nothing but libc.
 */
#define _GNU_SOURCE
#include <endian.h>
#include <errno.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#define BT_AF 31
#define BTPROTO_HCI_ 1
#define SOL_HCI_ 0
#define HCI_FILTER_ 2
#define HCI_CHANNEL_RAW_ 0
#define HCI_EVENT_PKT_ 0x04

#define HCIDEVUP_ _IOW('H', 201, int)
#define HCIGETDEVLIST_ _IOR('H', 210, int)
#define HCIGETDEVINFO_ _IOR('H', 211, int)

struct sockaddr_hci_ { sa_family_t hci_family; unsigned short hci_dev; unsigned short hci_channel; };
struct hci_filter_ { uint32_t type_mask; uint32_t event_mask[2]; uint16_t opcode; };
struct hci_dev_req_ { uint16_t dev_id; uint32_t dev_opt; };
struct hci_dev_list_req_ { uint16_t dev_num; struct hci_dev_req_ dev_req[]; };
typedef struct { uint8_t b[6]; } __attribute__((packed)) bdaddr_;
struct hci_dev_stats_ { uint32_t err_rx, err_tx, cmd_tx, evt_rx, acl_tx, acl_rx, sco_tx, sco_rx, byte_rx, byte_tx; };
struct hci_dev_info_ {
    uint16_t dev_id; char name[8]; bdaddr_ bdaddr; uint32_t flags; uint8_t type; uint8_t features[8];
    uint32_t pkt_type; uint32_t link_policy; uint32_t link_mode;
    uint16_t acl_mtu, acl_pkts, sco_mtu, sco_pkts; struct hci_dev_stats_ stat;
};

static int hci_socket(void) {
    int s = socket(BT_AF, SOCK_RAW | SOCK_CLOEXEC, BTPROTO_HCI_);
    if (s < 0) {
        fprintf(stderr, "cannot open a raw Bluetooth socket: %s%s\n", strerror(errno),
                errno == EPERM || errno == EACCES ? " (this helper needs the one-time permission grant)" : "");
        exit(1);
    }
    return s;
}

static void print_addr(const bdaddr_ *a) {
    printf("%02X:%02X:%02X:%02X:%02X:%02X", a->b[5], a->b[4], a->b[3], a->b[2], a->b[1], a->b[0]);
}

static int cmd_list(void) {
    int s = hci_socket();
    struct hci_dev_list_req_ *dl = calloc(1, sizeof(*dl) + 16 * sizeof(struct hci_dev_req_));
    dl->dev_num = 16;
    if (ioctl(s, HCIGETDEVLIST_, dl) < 0) { perror("list adapters"); return 1; }
    for (int i = 0; i < dl->dev_num; i++) {
        struct hci_dev_info_ di;
        memset(&di, 0, sizeof di);
        di.dev_id = dl->dev_req[i].dev_id;
        if (ioctl(s, HCIGETDEVINFO_, &di) < 0) continue;
        printf("hci%u ", di.dev_id);
        print_addr(&di.bdaddr);
        printf(" %s\n", (di.flags & 1) ? "UP" : "DOWN");  /* bit 0 = HCI_UP */
    }
    return 0;
}

static int parse_dev(const char *name) {
    if (strncmp(name, "hci", 3) != 0 || !name[3]) { fprintf(stderr, "bad adapter name: %s\n", name); exit(1); }
    return atoi(name + 3);
}

static int cmd_up(const char *name) {
    int s = hci_socket();
    if (ioctl(s, HCIDEVUP_, parse_dev(name)) < 0 && errno != EALREADY) {
        fprintf(stderr, "Can't init device %s: %s (%d)\n", name, strerror(errno), errno);
        return 1;
    }
    return 0;
}

static double now(void) {
    struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec + t.tv_nsec / 1e9;
}

static void set_event(struct hci_filter_ *f, int ev) { f->event_mask[ev >> 5] |= 1u << (ev & 31); }

#define MAX_SEEN 128
static uint8_t seen[MAX_SEEN][6];
static int nseen;

static void emit(const uint8_t *addr, const uint8_t *cls) {
    for (int i = 0; i < nseen; i++) if (!memcmp(seen[i], addr, 6)) return;
    if (nseen < MAX_SEEN) memcpy(seen[nseen++], addr, 6);
    bdaddr_ a; memcpy(a.b, addr, 6);
    print_addr(&a);
    printf(" 0x%02x%02x%02x\n", cls[2], cls[1], cls[0]);
    fflush(stdout);
}

/* A raw HCI socket sees the controller's replies to EVERY client's commands,
 * not just ours, so another program's scan (the desktop's Bluetooth UI, say)
 * can produce Command Status / Inquiry Complete events that aren't ours.
 * Ours is the first Command Status after we send; anything earlier, and any
 * later duplicate, belongs to somebody else. */
static int got_status;

/* One HCI packet from the controller. Returns -1 to keep reading, or the
 * process exit code once the scan is over. */
static int handle_event(const uint8_t *buf, ssize_t n) {
    if (n < 3 || buf[0] != HCI_EVENT_PKT_) return -1;
    uint8_t ev = buf[1];
    const uint8_t *pl = buf + 3;
    int plen = buf[2];
    if (n < 3 + plen) return -1;
    if (ev == 0x0f && plen >= 4) {                  /* Command Status for our Inquiry */
        if (got_status) return -1;                  /* someone else's */
        got_status = 1;
        if (pl[0] != 0x00) {
            if (pl[0] == 0x0c)
                fprintf(stderr, "inquiry refused: adapter is busy (already scanning -- close any Bluetooth settings window that is searching for devices)\n");
            else
                fprintf(stderr, "inquiry refused by adapter (status 0x%02x)\n", pl[0]);
            return 2;
        }
    } else if (ev == 0x01) {                        /* Inquiry Complete */
        if (!got_status) return -1;                 /* ended before ours began: not ours */
        return 0;
    } else if (ev == 0x02 && plen >= 1) {           /* Inquiry Result: 14 bytes per response, class at +9 */
        for (int i = 0; i < pl[0] && 1 + (i + 1) * 14 <= plen; i++) emit(pl + 1 + i * 14, pl + 1 + i * 14 + 9);
    } else if (ev == 0x22 && plen >= 1) {           /* with RSSI: 14 bytes each, class at +8 */
        for (int i = 0; i < pl[0] && 1 + (i + 1) * 14 <= plen; i++) emit(pl + 1 + i * 14, pl + 1 + i * 14 + 8);
    } else if (ev == 0x2f && plen >= 15) {          /* extended: one response, class at +8 */
        emit(pl + 1, pl + 1 + 8);
    }
    return -1;
}

static int cmd_scan(const char *name, double secs, uint32_t lap) {
    int dev = parse_dev(name);
    int s = hci_socket();

    struct hci_filter_ f;
    memset(&f, 0, sizeof f);
    f.type_mask = 1u << HCI_EVENT_PKT_;
    set_event(&f, 0x01); set_event(&f, 0x02); set_event(&f, 0x0f); set_event(&f, 0x22); set_event(&f, 0x2f);
    f.opcode = htole16(0x0401);  /* only our Inquiry's Command Status */
    if (setsockopt(s, SOL_HCI_, HCI_FILTER_, &f, sizeof f) < 0) { perror("filter"); return 1; }

    struct sockaddr_hci_ sa = { BT_AF, (unsigned short)dev, HCI_CHANNEL_RAW_ };
    if (bind(s, (struct sockaddr *)&sa, sizeof sa) < 0) { fprintf(stderr, "cannot use %s: %s\n", name, strerror(errno)); return 1; }

    int len = (int)(secs / 1.28 + 0.999);
    if (len < 1) len = 1;
    if (len > 48) len = 48;
    uint8_t cmd[] = { 0x01, 0x01, 0x04, 0x05, lap & 0xff, (lap >> 8) & 0xff, (lap >> 16) & 0xff, (uint8_t)len, 0x00 };
    if (write(s, cmd, sizeof cmd) != sizeof cmd) { perror("send inquiry"); return 1; }

    double deadline = now() + len * 1.28 + 2.0;
    for (;;) {
        double left = deadline - now();
        if (left <= 0) break;
        struct pollfd p = { s, POLLIN, 0 };
        int r = poll(&p, 1, (int)(left * 1000) + 1);
        if (r < 0) { if (errno == EINTR) continue; perror("poll"); return 1; }
        if (r == 0) break;
        uint8_t buf[300];
        ssize_t n = read(s, buf, sizeof buf);
        int rc = handle_event(buf, n);
        if (rc >= 0) return rc;
    }
    uint8_t cancel[] = { 0x01, 0x02, 0x04, 0x00 };  /* timed out first: stop the inquiry */
    if (write(s, cancel, sizeof cancel) < 0) { /* best effort */ }
    return 0;
}

/* Canned packets in the layouts the Bluetooth spec defines (the 0x22 one is
 * the exact 15-byte shape seen in a real capture). Lets the decoding be
 * tested without a controller: `wiimote-hci selftest`. */
static int cmd_selftest(void) {
    static const uint8_t remote22[] = { 0x04, 0x22, 15, 1, 0x6F,0xB9,0xD8,0xAB,0x17,0x00, 0x01, 0x00, 0x00,0x25,0x04, 0x00,0x00, 0xC4 };
    static const uint8_t two22[]    = { 0x04, 0x22, 29, 2, 0x8E,0xDA,0x0F,0xBD,0x6B,0x60, 0x01, 0x00, 0x0C,0x02,0x5A, 0x00,0x00, 0xB0,
                                        0x4B,0x54,0x4B,0xAB,0x17,0x00, 0x01, 0x00, 0x00,0x25,0x04, 0x00,0x00, 0xB0 };
    static const uint8_t plain02[]  = { 0x04, 0x02, 15, 1, 0xF1,0xE2,0xD3,0xC4,0xB5,0xA6, 0x01, 0x00, 0x00, 0x80,0x06,0x14, 0x00,0x00 };
    static const uint8_t refused[]  = { 0x04, 0x0f, 4, 0x0c, 1, 0x01, 0x04 };
    static const uint8_t accepted[] = { 0x04, 0x0f, 4, 0x00, 1, 0x01, 0x04 };
    static const uint8_t complete[] = { 0x04, 0x01, 1, 0x00 };
    static const uint8_t truncated[] = { 0x04, 0x22, 15, 1, 0x6F };
    static const uint8_t refused_other[] = { 0x04, 0x0f, 4, 0x0c, 1, 0x01, 0x04 };
    printf("foreign Inquiry Complete before our status -> rc %d\n", handle_event(complete, sizeof complete));
    printf("foreign refusal BEFORE ours is taken as ours (first status wins) -> rc %d\n", handle_event(refused_other, sizeof refused_other));
    got_status = 0;
    printf("our accepted status -> rc %d\n", handle_event(accepted, sizeof accepted));
    printf("remote22 -> rc %d\n", handle_event(remote22, sizeof remote22));
    printf("two22 -> rc %d\n", handle_event(two22, sizeof two22));
    printf("plain02 -> rc %d\n", handle_event(plain02, sizeof plain02));
    printf("duplicate remote22 -> rc %d\n", handle_event(remote22, sizeof remote22));
    printf("truncated -> rc %d\n", handle_event(truncated, sizeof truncated));
    printf("someone else's refusal AFTER ours was accepted -> rc %d\n", handle_event(refused, sizeof refused));
    printf("complete -> rc %d\n", handle_event(complete, sizeof complete));
    return 0;
}

int main(int argc, char **argv) {
    if (argc >= 2 && !strcmp(argv[1], "list")) return cmd_list();
    if (argc >= 3 && !strcmp(argv[1], "up")) return cmd_up(argv[2]);
    if (argc >= 2 && !strcmp(argv[1], "selftest")) return cmd_selftest();
    if (argc >= 5 && !strcmp(argv[1], "scan")) return cmd_scan(argv[2], atof(argv[3]), (uint32_t)strtoul(argv[4], NULL, 16));
    fprintf(stderr, "usage: %s list | up hciN | scan hciN SECS LAPHEX\n", argv[0]);
    return 64;
}

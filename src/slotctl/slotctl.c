/* slotctl — A/B slot pointer for cronos-min, usable from the initramfs shell.
 *
 * Why C and not a shell script: this runs before `switch_root`, decides whether
 * the device boots at all, and has to be correct when the eMMC returned a
 * half-written 4 KB page. It is 300 lines of freestanding C with no libc state,
 * statically linkable (`-static`) for the arm64 initramfs, and unit-testable on
 * the build host — all three matter more than the ~40 lines of shell it replaces.
 *
 * NOTE ON ARCHITECTURE: the initramfs is executed by the *arm64* kernel, so this
 * binary and busybox in the initramfs must be aarch64. The rootfs is armv7.
 * Two ABIs on one device, and the initramfs is the one people forget.
 *
 * On-disk format (/store/slot.ptr, 4096 bytes, two independent copies):
 *
 *   offset 0     copy 0: struct slot_rec (24 bytes)
 *   offset 2048  copy 1: struct slot_rec (24 bytes)
 *
 *   struct slot_rec {
 *       char magic[4];   // "CRS1"
 *       u8   version;    // 1
 *       u8   active;     // 0 = A, 1 = B
 *       u8   trials;     // 0..3 = boot attempts left, 255 = committed
 *       u8   flags;      // bit0 = slot A bad, bit1 = slot B bad
 *       u32  seq;        // increases on every write; newest valid copy wins
 *       u32  crc;        // crc32 over bytes [0,20)
 *   }
 *
 * Command exit codes are the API:
 *   0  a slot is usable (prints SLOT_ACTIVE=<n> SLOT_TRIALS=<n>)
 *   2  no valid copy on disk (fresh install, or unrecoverable corruption)
 *   3  both slots are marked bad -> caller must stay in the rescue shell
 *   4  usage / I/O error
 */

#define _GNU_SOURCE   /* O_DIRECTORY */
#include <errno.h>
#include <fcntl.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define SLOT_FILE_DEFAULT "/store/slot.ptr"
#define SLOT_FILE_BYTES   4096
#define REC_OFFSET_0      0
#define REC_OFFSET_1      2048
#define REC_SIZE          16
#define MAGIC             "CRS1"
#define VERSION           1
#define TRIALS_COMMITTED  0xFF
#define TRIALS_NEW        3

#define FLAG_A_BAD        0x01
#define FLAG_B_BAD        0x02

struct slot_rec {
    char     magic[4];
    uint8_t  version;
    uint8_t  active;
    uint8_t  trials;
    uint8_t  flags;
    uint32_t seq;
    uint32_t crc;
} __attribute__((packed));

_Static_assert(sizeof(struct slot_rec) == REC_SIZE, "slot_rec must be 16 bytes");

/* The checksum covers magic..seq, i.e. everything before the crc field. It was
 * 20 when the record had a padding hole; after packing it is 12. A span that
 * runs past the struct would read adjacent memory and the crc would "work" on
 * write and fail on read — the exact class of bug this file exists to avoid. */
#define REC_CRC_SPAN ((size_t)offsetof(struct slot_rec, crc))
_Static_assert(REC_SIZE - REC_CRC_SPAN == 4, "crc must be the last 4 bytes");

static uint32_t crc32_bytes(const uint8_t *p, size_t n)
{
    uint32_t crc = 0xFFFFFFFFu;
    for (size_t i = 0; i < n; i++) {
        crc ^= p[i];
        for (int b = 0; b < 8; b++)
            crc = (crc >> 1) ^ (0xEDB88320u & (uint32_t)(-(int32_t)(crc & 1u)));
    }
    return ~crc;
}

static int rec_valid(const struct slot_rec *r)
{
    if (memcmp(r->magic, MAGIC, 4) != 0)
        return 0;
    if (r->version != VERSION)
        return 0;
    if (r->active > 1)
        return 0;
    if (r->trials != TRIALS_COMMITTED && r->trials > TRIALS_NEW)
        return 0;
    if (r->flags & ~(FLAG_A_BAD | FLAG_B_BAD))
        return 0;
    return crc32_bytes((const uint8_t *)r, REC_CRC_SPAN) == r->crc;
}

static void rec_seal(struct slot_rec *r)
{
    r->crc = crc32_bytes((const uint8_t *)r, REC_CRC_SPAN);
}

static void rec_init(struct slot_rec *r, uint8_t active, uint8_t trials,
                     uint8_t flags, uint32_t seq)
{
    memcpy(r->magic, MAGIC, 4);
    r->version = VERSION;
    r->active = active;
    r->trials = trials;
    r->flags = flags;
    r->seq = seq;
    rec_seal(r);
}

struct store {
    uint8_t buf[SLOT_FILE_BYTES];
    struct slot_rec rec[2];
    int valid[2];
    int have;       /* at least one valid copy */
    int best;       /* index of the authoritative copy, -1 if none */
};

static int store_load(const char *path, struct store *s)
{
    memset(s, 0, sizeof(*s));
    int fd = open(path, O_RDONLY);
    if (fd < 0) {
        if (errno == ENOENT)
            return 2;   /* fresh install */
        fprintf(stderr, "slotctl: open %s: %s\n", path, strerror(errno));
        return 4;
    }
    ssize_t got = read(fd, s->buf, sizeof(s->buf));
    close(fd);
    if (got < 0) {
        fprintf(stderr, "slotctl: read %s: %s\n", path, strerror(errno));
        return 4;
    }
    /* A short file can still hold copy 0; only treat it as corrupt if the
     * record itself is incomplete. */
    for (int i = 0; i < 2; i++) {
        size_t off = i ? REC_OFFSET_1 : REC_OFFSET_0;
        if ((size_t)got >= off + REC_SIZE) {
            memcpy(&s->rec[i], s->buf + off, REC_SIZE);
            s->valid[i] = rec_valid(&s->rec[i]);
        }
    }
    s->have = s->valid[0] || s->valid[1];
    if (!s->have)
        return 2;
    s->best = -1;
    for (int i = 0; i < 2; i++) {
        if (!s->valid[i])
            continue;
        /* newest seq wins; on a tie prefer copy 0 deterministically */
        if (s->best < 0 || s->rec[i].seq > s->rec[s->best].seq)
            s->best = i;
    }
    if (s->valid[0] && s->valid[1] && s->rec[0].seq == s->rec[1].seq)
        s->best = 0;
    return 0;
}

static int store_write(const char *path, const struct slot_rec *r)
{
    /* Write to a fresh temp file, fsync, rename. A half-written slot.ptr is the
     * one corruption that bricks a unit into the rescue shell, and rename is
     * atomic on ext4. */
    char tmp[512];
    if (snprintf(tmp, sizeof(tmp), "%s.new", path) >= (int)sizeof(tmp)) {
        fprintf(stderr, "slotctl: path too long\n");
        return 4;
    }
    uint8_t buf[SLOT_FILE_BYTES];
    memset(buf, 0, sizeof(buf));
    memcpy(buf + REC_OFFSET_0, r, REC_SIZE);
    memcpy(buf + REC_OFFSET_1, r, REC_SIZE);

    int fd = open(tmp, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd < 0) {
        fprintf(stderr, "slotctl: create %s: %s\n", tmp, strerror(errno));
        return 4;
    }
    ssize_t n = write(fd, buf, sizeof(buf));
    if (n != (ssize_t)sizeof(buf)) {
        fprintf(stderr, "slotctl: write %s: %s\n", tmp, n < 0 ? strerror(errno) : "short write");
        close(fd);
        unlink(tmp);
        return 4;
    }
    if (fsync(fd) != 0) {
        fprintf(stderr, "slotctl: fsync %s: %s\n", tmp, strerror(errno));
        close(fd);
        unlink(tmp);
        return 4;
    }
    close(fd);
    if (rename(tmp, path) != 0) {
        fprintf(stderr, "slotctl: rename %s: %s\n", tmp, strerror(errno));
        unlink(tmp);
        return 4;
    }
    /* fsync the directory so the rename survives a power cut */
    char dir[512];
    snprintf(dir, sizeof(dir), "%s", path);
    char *slash = strrchr(dir, '/');
    if (slash) {
        *slash = '\0';
        int dfd = open(dir[0] ? dir : "/", O_RDONLY | O_DIRECTORY);
        if (dfd >= 0) {
            fsync(dfd);
            close(dfd);
        }
    }
    return 0;
}

static void print_state(const struct slot_rec *r)
{
    printf("SLOT_ACTIVE=%u\n", r->active);
    printf("SLOT_TRIALS=%u\n", r->trials);
    printf("SLOT_FLAGS=%u\n", r->flags);
    printf("SLOT_SEQ=%u\n", r->seq);
}

static int other_slot(uint8_t a) { return a ? 0 : 1; }
static uint8_t bad_flag(uint8_t slot) { return slot ? FLAG_B_BAD : FLAG_A_BAD; }

/* --- commands ------------------------------------------------------------ */

static int cmd_init(const char *path)
{
    struct slot_rec r;
    rec_init(&r, 0, TRIALS_COMMITTED, 0, 1);
    int rc = store_write(path, &r);
    if (rc)
        return rc;
    printf("initialised %s: slot A committed\n", path);
    return 0;
}

/* Called by initramfs /init on every boot. */
static int cmd_boot(const char *path)
{
    struct store s;
    int rc = store_load(path, &s);
    if (rc == 2) {
        fprintf(stderr, "slotctl: no valid slot record in %s\n", path);
        return 2;
    }
    if (rc)
        return rc;

    struct slot_rec r = s.rec[s.best];

    /* Both slots condemned: do not pretend one of them is bootable. */
    if ((r.flags & (FLAG_A_BAD | FLAG_B_BAD)) == (FLAG_A_BAD | FLAG_B_BAD)) {
        fprintf(stderr, "slotctl: both slots are marked bad; rescue only\n");
        return 3;
    }

    if (r.trials == TRIALS_COMMITTED) {
        print_state(&r);
        return 0;
    }

    if (r.trials > 0) {
        r.trials--;
        r.seq++;
        rec_seal(&r);
        if ((rc = store_write(path, &r)))
            return rc;
        printf("trial boot: %u attempt(s) left on slot %c\n", r.trials,
               r.active ? 'B' : 'A');
        print_state(&r);
        return 0;
    }

    /* Trials exhausted: this slot never reached a commit. Mark it bad. */
    r.flags |= bad_flag(r.active);
    uint8_t next = (uint8_t)other_slot(r.active);
    if (r.flags & bad_flag(next)) {
        fprintf(stderr, "slotctl: both slots marked bad; staying in rescue\n");
        return 3;
    }
    fprintf(stderr, "slotctl: slot %c failed to commit in %u boots; "
                    "reverting to slot %c\n", r.active ? 'B' : 'A', TRIALS_NEW,
                    next ? 'B' : 'A');
    r.active = next;
    r.trials = TRIALS_COMMITTED;
    r.seq++;
    rec_seal(&r);
    if ((rc = store_write(path, &r)))
        return rc;
    print_state(&r);
    return 0;
}

/* Called by cronosd after a healthy uptime. */
static int cmd_commit(const char *path)
{
    struct store s;
    int rc = store_load(path, &s);
    if (rc) {
        if (rc == 2) {
            fprintf(stderr, "slotctl: no slot record to commit\n");
            return 2;
        }
        return rc;
    }
    struct slot_rec r = s.rec[s.best];
    if (r.trials == TRIALS_COMMITTED) {
        printf("slot %c already committed\n", r.active ? 'B' : 'A');
        return 0;
    }
    r.trials = TRIALS_COMMITTED;
    r.seq++;
    rec_seal(&r);
    if ((rc = store_write(path, &r)))
        return rc;
    printf("committed slot %c\n", r.active ? 'B' : 'A');
    print_state(&r);
    return 0;
}

/* Called by the update path after the inactive slot has been written+verified. */
static int cmd_pending(const char *path)
{
    struct store s;
    int rc = store_load(path, &s);
    struct slot_rec r;
    if (rc == 2) {
        rec_init(&r, 1, TRIALS_NEW, 0, 1);
        r.trials = TRIALS_NEW;
        rec_seal(&r);
        if ((rc = store_write(path, &r)))
            return rc;
        print_state(&r);
        return 0;
    }
    if (rc)
        return rc;
    r = s.rec[s.best];
    uint8_t next = (uint8_t)other_slot(r.active);
    r.flags &= (uint8_t)~bad_flag(next);   /* a new image gets a fresh chance */
    r.active = next;
    r.trials = TRIALS_NEW;
    r.seq++;
    rec_seal(&r);
    if ((rc = store_write(path, &r)))
        return rc;
    printf("slot %c armed with %u trial boots\n", r.active ? 'B' : 'A', TRIALS_NEW);
    print_state(&r);
    return 0;
}

/* Mark the active slot bad and move to the other one. This is what a failed
 * trial eventually does on its own; exposing it lets a person (or a test) force
 * a switch without waiting three boots, and it is the only way both slots can
 * legitimately reach the bad state. */
static int cmd_fail(const char *path)
{
    struct store s;
    int rc = store_load(path, &s);
    if (rc)
        return rc == 2 ? 2 : rc;
    struct slot_rec r = s.rec[s.best];
    r.flags |= bad_flag(r.active);
    uint8_t next = (uint8_t)other_slot(r.active);
    r.seq++;
    if (r.flags & bad_flag(next)) {
        rec_seal(&r);
        if ((rc = store_write(path, &r)))
            return rc;
        fprintf(stderr, "slotctl: both slots are bad now; staying in rescue\n");
        print_state(&r);
        return 3;
    }
    r.active = next;
    r.trials = TRIALS_COMMITTED;
    rec_seal(&r);
    if ((rc = store_write(path, &r)))
        return rc;
    fprintf(stderr, "slot %c marked bad; switched to slot %c\n",
            r.active ? 'A' : 'B', r.active ? 'B' : 'A');
    print_state(&r);
    return 0;
}

static int cmd_get(const char *path)
{
    struct store s;
    int rc = store_load(path, &s);
    if (rc == 2) {
        fprintf(stderr, "slotctl: no valid slot record\n");
        return 2;
    }
    if (rc)
        return rc;
    printf("active=%c trials=%u flags=%u seq=%u copy=%d valid=%d,%d\n",
           s.rec[s.best].active ? 'B' : 'A', s.rec[s.best].trials,
           s.rec[s.best].flags, s.rec[s.best].seq, s.best, s.valid[0], s.valid[1]);
    return 0;
}

static int cmd_selftest(void)
{
    /* Runs on the host in `make test`; proves the CRC and record layout. */
    struct slot_rec r;
    rec_init(&r, 1, 2, 0, 7);
    if (!rec_valid(&r)) {
        fprintf(stderr, "selftest: freshly sealed record invalid\n");
        return 1;
    }
    r.trials ^= 1;
    if (rec_valid(&r)) {
        fprintf(stderr, "selftest: corrupted record accepted\n");
        return 1;
    }
    r.trials ^= 1;
    if (crc32_bytes((const uint8_t *)"123456789", 9) != 0xCBF43926u) {
        fprintf(stderr, "selftest: crc32 check vector failed\n");
        return 1;
    }
    printf("selftest: ok\n");
    return 0;
}

static void usage(void)
{
    fprintf(stderr,
            "usage: slotctl [-f FILE] <command>\n"
            "  init      create a fresh record (slot A, committed)\n"
            "  boot      initramfs: consume a trial, or revert (exit 2/3 on trouble)\n"
            "  pending   arm the inactive slot with %d trial boots\n"
            "  commit    mark the active slot good\n"
            "  fail      mark the active slot bad and switch (exit 3 if both bad)\n"
            "  get       print one-line state\n"
            "  selftest  verify crc/layout (build host only)\n"
            "Exit: 0 ok, 2 no record, 3 both slots bad, 4 error.\n",
            TRIALS_NEW);
}

int main(int argc, char **argv)
{
    const char *path = getenv("SLOT_PTR");
    if (!path || !*path)
        path = SLOT_FILE_DEFAULT;

    int i = 1;
    while (i < argc && argv[i][0] == '-') {
        if (strcmp(argv[i], "-f") == 0 && i + 1 < argc) {
            path = argv[++i];
            i++;
        } else if (strcmp(argv[i], "-h") == 0 || strcmp(argv[i], "--help") == 0) {
            usage();
            return 0;
        } else {
            usage();
            return 4;
        }
    }
    if (i >= argc) {
        usage();
        return 4;
    }
    const char *cmd = argv[i];

    if (strcmp(cmd, "init") == 0)      return cmd_init(path);
    if (strcmp(cmd, "boot") == 0)      return cmd_boot(path);
    if (strcmp(cmd, "commit") == 0)    return cmd_commit(path);
    if (strcmp(cmd, "pending") == 0)   return cmd_pending(path);
    if (strcmp(cmd, "fail") == 0)      return cmd_fail(path);
    if (strcmp(cmd, "get") == 0)       return cmd_get(path);
    if (strcmp(cmd, "selftest") == 0)  return cmd_selftest();

    usage();
    return 4;
}

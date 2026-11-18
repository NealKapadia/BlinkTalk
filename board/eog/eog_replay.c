/* Replays a recording (CSV: time, volts at 500 Hz, as exported by the AFE4950 GUI) through the
   streaming recognizer and prints "onset_sample,command" for every command it decides.
   Build: gcc -O2 -o eog_replay eog_replay.c eog.c -lm
   Usage: ./eog_replay ../../data/recordings/blink_every_3s.csv                                    */
#include "eog.h"
#include <stdio.h>

int main(int argc, char **argv) {
    if (argc < 2) { fprintf(stderr, "usage: %s file.csv\n", argv[0]); return 1; }
    FILE *fp = fopen(argv[1], "r");
    if (!fp) { perror("open"); return 1; }
    static eog_t e;
    eog_init(&e);
    char line[256];
    if (!fgets(line, sizeof line, fp)) return 1; /* header */
    const char *names[] = {"none", "blink", "left", "right", "select", "power", "sos"};
    double last = 0;
    while (fgets(line, sizeof line, fp)) {
        double t, v;
        if (sscanf(line, "%lf,%lf", &t, &v) == 2) last = v; /* blank value -> hold previous */
        eog_event_t ev = eog_push(&e, (float)last);
        if (ev != EOG_NONE)
            printf("%lld,%s\n", (long long)e.last_event_onset, names[ev]);
    }
    fclose(fp);
    return 0;
}

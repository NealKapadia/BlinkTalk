/* Shared-library API for Python (ctypes), used by recognizer.py and blinktalk_board.py.
   Build on the board: gcc -O2 -shared -fPIC -o libeog.so eog.c eog_api.c -lm */
#include "eog.h"
#include <stdlib.h>

int eog_state_size(void) { return (int)sizeof(eog_t); }

eog_t *eog_create(void) {
    eog_t *e = (eog_t *)malloc(sizeof(eog_t));
    if (e) eog_init(e);
    return e;
}

void eog_destroy(eog_t *e) { free(e); }

void eog_set_threshold(eog_t *e, double thr) { e->event_thr = thr; }
void eog_set_swap_lr(eog_t *e, int swap) { e->swap_lr = swap ? 1 : 0; }

/* Push n samples (volts). Writes up to max events: codes (eog_event_t) and onset sample
   indices. Returns the number of events written. */
int eog_push_block(eog_t *e, const float *x, int n, int *codes, long long *onsets, int max) {
    int k = 0;
    for (int i = 0; i < n; i++) {
        eog_event_t ev = eog_push(e, x[i]);
        if (ev != EOG_NONE && k < max) { codes[k] = (int)ev; onsets[k] = (long long)e->last_event_onset; k++; }
    }
    return k;
}

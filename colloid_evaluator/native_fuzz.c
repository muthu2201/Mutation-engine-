/*
 * Differential fuzz driver for libshopnative (evaluator-owned, never visible to candidates).
 *
 * The baseline sources are compiled with every public symbol renamed (base_levenshtein,
 * base_shop_score_batch, ...) via -D flags, the candidate sources are compiled as-is, and
 * both are linked into this one binary. For N random inputs the driver calls both versions
 * and requires identical results:
 *
 *   levenshtein        exact integer equality
 *   fuzzy_similarity   |a-b| <= 1e-12 * max(1,|a|)
 *   shop_tokenize      same count, same tokens
 *   shop_score_batch   every score within rel 1e-9 / abs 1e-12, same return code
 *
 * Inputs mix ASCII letters, digits, punctuation, whitespace runs, empty strings, long
 * strings and bytes >= 0x80 (UTF-8 fragments), because tokenisers and edit-distance code
 * typically break on exactly those. Built with -fsanitize=address,undefined for the deep
 * (L6) pass so out-of-bounds accesses and undefined behaviour abort the run.
 *
 * Usage: native_fuzz <seed> <iterations>; exit 0 = equivalent, 1 = mismatch, 2 = usage.
 */
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "shopnative.h"

int base_levenshtein(const char *a, const char *b);
double base_fuzzy_similarity(const char *a, const char *b);
int base_shop_tokenize(const char *text, char ***out);
void base_shop_free_tokens(char **tokens, int count);
int base_shop_score_batch(const char **terms, int nterms, const char **docs, int ndocs, double *out);

static uint64_t state;

static uint64_t next_u64(void) {
    state ^= state << 13;
    state ^= state >> 7;
    state ^= state << 17;
    return state;
}

static int rnd(int n) { return (int)(next_u64() % (uint64_t)n); }

static const char *WORDS[] = {"leather", "backpack", "steel", "kettle", "waterproof", "lamp", "oak", "desk", "mug", "eco",
                              "premium", "silk", "glove", "smart", "watch", "a", "of", "the", "x2", "lether", "bakpack"};

static void random_string(char *buf, int maxlen) {
    int mode = rnd(4);
    int len = rnd(maxlen);
    int pos = 0;
    if (mode == 0) { /* word salad from the domain vocabulary, with typos */
        while (pos < len - 12) {
            const char *w = WORDS[rnd((int)(sizeof WORDS / sizeof WORDS[0]))];
            int wl = (int)strlen(w);
            for (int i = 0; i < wl; i++) {
                char c = w[i];
                if (rnd(10) == 0) c = (char)('a' + rnd(26));
                if (rnd(6) == 0) c = (char)(c - 32 * (c >= 'a' && c <= 'z'));
                buf[pos++] = c;
            }
            buf[pos++] = rnd(5) == 0 ? ',' : ' ';
        }
    } else {
        for (; pos < len; pos++) {
            int r = rnd(100);
            if (r < 60) buf[pos] = (char)('a' + rnd(26));
            else if (r < 70) buf[pos] = (char)('A' + rnd(26));
            else if (r < 78) buf[pos] = (char)('0' + rnd(10));
            else if (r < 88) buf[pos] = ' ';
            else if (r < 95) buf[pos] = "-_.,;:!?/()"[rnd(11)];
            else buf[pos] = (char)(0x80 + rnd(0x7f));
        }
    }
    buf[pos] = '\0';
}

static int close_enough(double a, double b, double rel, double abs_tol) {
    if (a == b) return 1;
    if (isnan(a) || isnan(b)) return 0;
    double diff = fabs(a - b);
    double scale = fabs(a) > fabs(b) ? fabs(a) : fabs(b);
    return diff <= abs_tol || diff <= rel * scale;
}

int main(int argc, char **argv) {
    if (argc != 3) {
        fprintf(stderr, "usage: %s seed iterations\n", argv[0]);
        return 2;
    }
    state = strtoull(argv[1], NULL, 10) * 2654435761u + 0x9E3779B97F4A7C15ull;
    if (state == 0) state = 1;
    long iterations = strtol(argv[2], NULL, 10);
    static char a[512], b[512];
    for (long it = 0; it < iterations; it++) {
        random_string(a, 48);
        random_string(b, 48);
        int la = levenshtein(a, b), lb = base_levenshtein(a, b);
        if (la != lb) {
            printf("MISMATCH levenshtein(\"%s\", \"%s\"): candidate %d, baseline %d\n", a, b, la, lb);
            return 1;
        }
        double fa = fuzzy_similarity(a, b), fb = base_fuzzy_similarity(a, b);
        if (!close_enough(fa, fb, 1e-12, 1e-12)) {
            printf("MISMATCH fuzzy_similarity(\"%s\", \"%s\"): candidate %.17g, baseline %.17g\n", a, b, fa, fb);
            return 1;
        }
        random_string(a, 400);
        char **ta = NULL, **tb = NULL;
        int na = shop_tokenize(a, &ta), nb = base_shop_tokenize(a, &tb);
        if (na != nb) {
            printf("MISMATCH shop_tokenize(\"%s\"): candidate %d tokens, baseline %d\n", a, na, nb);
            return 1;
        }
        for (int i = 0; i < na; i++) {
            if (strcmp(ta[i], tb[i]) != 0) {
                printf("MISMATCH shop_tokenize(\"%s\") token %d: \"%s\" vs \"%s\"\n", a, i, ta[i], tb[i]);
                return 1;
            }
        }
        if (na >= 0) shop_free_tokens(ta, na);
        if (nb >= 0) base_shop_free_tokens(tb, nb);

        if (it % 10 == 0) {
            int nterms = rnd(4), ndocs = 1 + rnd(40);
            char *terms[4], *docs[40];
            for (int t = 0; t < nterms; t++) {
                terms[t] = malloc(32);
                random_string(terms[t], 14);
            }
            for (int d = 0; d < ndocs; d++) {
                docs[d] = malloc(400);
                random_string(docs[d], 300);
            }
            double *sa = calloc(ndocs, sizeof(double)), *sb = calloc(ndocs, sizeof(double));
            int ra = shop_score_batch((const char **)terms, nterms, (const char **)docs, ndocs, sa);
            int rb = base_shop_score_batch((const char **)terms, nterms, (const char **)docs, ndocs, sb);
            if (ra != rb) {
                printf("MISMATCH shop_score_batch return code: candidate %d, baseline %d\n", ra, rb);
                return 1;
            }
            for (int d = 0; d < ndocs; d++) {
                if (!close_enough(sa[d], sb[d], 1e-9, 1e-12)) {
                    printf("MISMATCH shop_score_batch doc %d (\"%s\"): candidate %.17g, baseline %.17g\n", d, docs[d], sa[d], sb[d]);
                    return 1;
                }
            }
            free(sa);
            free(sb);
            for (int t = 0; t < nterms; t++) free(terms[t]);
            for (int d = 0; d < ndocs; d++) free(docs[d]);
        }
    }
    printf("OK %ld iterations\n", iterations);
    return 0;
}

#include <stdlib.h>
#include <string.h>

#include "shopnative.h"

int levenshtein(const char *a, const char *b) {
    int n = (int)strlen(a);
    int m = (int)strlen(b);
    int *d = malloc(sizeof(int) * (n + 1) * (m + 1));
    if (d == NULL) {
        return -1;
    }
    for (int i = 0; i <= n; i++) {
        d[i * (m + 1)] = i;
    }
    for (int j = 0; j <= m; j++) {
        d[j] = j;
    }
    for (int i = 1; i <= n; i++) {
        for (int j = 1; j <= m; j++) {
            int cost = a[i - 1] == b[j - 1] ? 0 : 1;
            int deletion = d[(i - 1) * (m + 1) + j] + 1;
            int insertion = d[i * (m + 1) + (j - 1)] + 1;
            int substitution = d[(i - 1) * (m + 1) + (j - 1)] + cost;
            int best = deletion < insertion ? deletion : insertion;
            d[i * (m + 1) + j] = best < substitution ? best : substitution;
        }
    }
    int result = d[n * (m + 1) + m];
    free(d);
    return result;
}

double fuzzy_similarity(const char *a, const char *b) {
    int la = (int)strlen(a);
    int lb = (int)strlen(b);
    int longest = la > lb ? la : lb;
    if (longest == 0) {
        return 0.0;
    }
    int distance = levenshtein(a, b);
    if (distance < 0) {
        return 0.0;
    }
    return 1.0 - (double)distance / (double)longest;
}

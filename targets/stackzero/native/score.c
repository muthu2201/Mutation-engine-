#include <math.h>
#include <stdlib.h>

#include "shopnative.h"

#define MATCH_THRESHOLD 0.75
#define BM25_K1 1.2
#define BM25_B 0.75

int shop_score_batch(const char **terms, int nterms, const char **docs, int ndocs, double *out) {
    if (ndocs <= 0) {
        return 0;
    }
    char ***doc_tokens = malloc(sizeof(char **) * ndocs);
    int *doc_lengths = malloc(sizeof(int) * ndocs);
    double *tf = calloc((size_t)ndocs * (nterms > 0 ? nterms : 1), sizeof(double));
    int *df = calloc(nterms > 0 ? nterms : 1, sizeof(int));
    if (doc_tokens == NULL || doc_lengths == NULL || tf == NULL || df == NULL) {
        free(doc_tokens);
        free(doc_lengths);
        free(tf);
        free(df);
        return -1;
    }
    int tokenized = 0;
    double total_length = 0.0;
    int status = 0;
    for (int d = 0; d < ndocs; d++) {
        int count = shop_tokenize(docs[d], &doc_tokens[d]);
        if (count < 0) {
            status = -1;
            break;
        }
        doc_lengths[d] = count;
        total_length += count;
        tokenized++;
    }
    if (status == 0) {
        double average_length = total_length / ndocs;
        if (average_length <= 0.0) {
            average_length = 1.0;
        }
        for (int d = 0; d < ndocs; d++) {
            for (int t = 0; t < nterms; t++) {
                double frequency = 0.0;
                for (int k = 0; k < doc_lengths[d]; k++) {
                    double similarity = fuzzy_similarity(terms[t], doc_tokens[d][k]);
                    if (similarity >= MATCH_THRESHOLD) {
                        frequency += similarity;
                    }
                }
                tf[d * nterms + t] = frequency;
                if (frequency > 0.0) {
                    df[t]++;
                }
            }
        }
        for (int d = 0; d < ndocs; d++) {
            double score = 0.0;
            double norm = BM25_K1 * (1.0 - BM25_B + BM25_B * doc_lengths[d] / average_length);
            for (int t = 0; t < nterms; t++) {
                double frequency = tf[d * nterms + t];
                if (frequency <= 0.0) {
                    continue;
                }
                double idf = log(1.0 + (ndocs - df[t] + 0.5) / (df[t] + 0.5));
                score += idf * frequency * (BM25_K1 + 1.0) / (frequency + norm);
            }
            out[d] = score;
        }
    }
    for (int d = 0; d < tokenized; d++) {
        shop_free_tokens(doc_tokens[d], doc_lengths[d]);
    }
    free(doc_tokens);
    free(doc_lengths);
    free(tf);
    free(df);
    return status;
}

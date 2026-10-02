#ifndef SHOPNATIVE_H
#define SHOPNATIVE_H

/* Split text into lower-case alphanumeric tokens. Returns the token count and stores a
 * newly allocated array of newly allocated strings in *out (free with shop_free_tokens). */
int shop_tokenize(const char *text, char ***out);
void shop_free_tokens(char **tokens, int count);

/* Edit distance between two strings (insertions, deletions, substitutions cost 1). */
int levenshtein(const char *a, const char *b);

/* 1 - distance / max(len(a), len(b)); 1.0 for identical strings, 0.0 for empty input. */
double fuzzy_similarity(const char *a, const char *b);

/* Typo-tolerant BM25: score each document for the query terms. A document token matches a
 * term when their fuzzy similarity is at least 0.75; the match contributes its similarity
 * to the term frequency. Writes ndocs scores to out. Returns 0 on success. */
int shop_score_batch(const char **terms, int nterms, const char **docs, int ndocs, double *out);

#endif

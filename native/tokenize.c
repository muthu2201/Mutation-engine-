#include <ctype.h>
#include <stdlib.h>
#include <string.h>

#include "shopnative.h"

int shop_tokenize(const char *text, char ***out) {
    int capacity = 8;
    int count = 0;
    char **tokens = malloc(sizeof(char *) * capacity);
    if (tokens == NULL) {
        return -1;
    }
    size_t len = strlen(text);
    size_t i = 0;
    while (i < len) {
        while (i < len && !isalnum((unsigned char)text[i])) {
            i++;
        }
        size_t start = i;
        while (i < len && isalnum((unsigned char)text[i])) {
            i++;
        }
        if (i > start) {
            char *token = malloc(i - start + 1);
            if (token == NULL) {
                shop_free_tokens(tokens, count);
                return -1;
            }
            for (size_t k = start; k < i; k++) {
                token[k - start] = (char)tolower((unsigned char)text[k]);
            }
            token[i - start] = '\0';
            if (count == capacity) {
                capacity *= 2;
                char **grown = realloc(tokens, sizeof(char *) * capacity);
                if (grown == NULL) {
                    free(token);
                    shop_free_tokens(tokens, count);
                    return -1;
                }
                tokens = grown;
            }
            tokens[count++] = token;
        }
    }
    *out = tokens;
    return count;
}

void shop_free_tokens(char **tokens, int count) {
    for (int i = 0; i < count; i++) {
        free(tokens[i]);
    }
    free(tokens);
}

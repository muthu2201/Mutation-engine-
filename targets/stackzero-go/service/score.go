package main

import "math"

// Typo-tolerant BM25 ranking: the same algorithm, constants and floating-point operation
// order as the C library of the Python reference (targets/stackzero/native), so both
// implementations rank identically.

const (
	matchThreshold = 0.75
	bm25K1         = 1.2
	bm25B          = 0.75
)

// shopTokenize splits text into lower-case ASCII alphanumeric tokens.
func shopTokenize(text string) []string {
	var tokens []string
	i := 0
	for i < len(text) {
		for i < len(text) && !isAlnum(text[i]) {
			i++
		}
		start := i
		for i < len(text) && isAlnum(text[i]) {
			i++
		}
		if i > start {
			token := make([]byte, i-start)
			for k := start; k < i; k++ {
				token[k-start] = toLower(text[k])
			}
			tokens = append(tokens, string(token))
		}
	}
	return tokens
}

func isAlnum(c byte) bool {
	return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9')
}

func toLower(c byte) byte {
	if c >= 'A' && c <= 'Z' {
		return c + ('a' - 'A')
	}
	return c
}

// levenshtein is the edit distance between two strings (insertions, deletions and
// substitutions cost 1), over bytes.
func levenshtein(a, b string) int {
	n, m := len(a), len(b)
	d := make([]int, (n+1)*(m+1))
	for i := 0; i <= n; i++ {
		d[i*(m+1)] = i
	}
	for j := 0; j <= m; j++ {
		d[j] = j
	}
	for i := 1; i <= n; i++ {
		for j := 1; j <= m; j++ {
			cost := 1
			if a[i-1] == b[j-1] {
				cost = 0
			}
			deletion := d[(i-1)*(m+1)+j] + 1
			insertion := d[i*(m+1)+(j-1)] + 1
			substitution := d[(i-1)*(m+1)+(j-1)] + cost
			best := insertion
			if deletion < insertion {
				best = deletion
			}
			if substitution < best {
				best = substitution
			}
			d[i*(m+1)+j] = best
		}
	}
	return d[n*(m+1)+m]
}

// fuzzySimilarity is 1 - distance / max(len(a), len(b)); 1.0 for identical strings and
// 0.0 for empty input.
func fuzzySimilarity(a, b string) float64 {
	longest := len(a)
	if len(b) > longest {
		longest = len(b)
	}
	if longest == 0 {
		return 0.0
	}
	return 1.0 - float64(levenshtein(a, b))/float64(longest)
}

// scoreBatch scores each document for the query terms. A document token matches a term
// when their fuzzy similarity is at least 0.75; the match contributes its similarity to
// the term frequency.
func scoreBatch(terms []string, docs []string) []float64 {
	ndocs, nterms := len(docs), len(terms)
	out := make([]float64, ndocs)
	if ndocs == 0 {
		return out
	}
	docTokens := make([][]string, ndocs)
	docLengths := make([]int, ndocs)
	tf := make([]float64, ndocs*max(nterms, 1))
	df := make([]int, max(nterms, 1))
	totalLength := 0.0
	for d := 0; d < ndocs; d++ {
		docTokens[d] = shopTokenize(docs[d])
		docLengths[d] = len(docTokens[d])
		totalLength += float64(docLengths[d])
	}
	averageLength := totalLength / float64(ndocs)
	if averageLength <= 0.0 {
		averageLength = 1.0
	}
	for d := 0; d < ndocs; d++ {
		for t := 0; t < nterms; t++ {
			frequency := 0.0
			for k := 0; k < docLengths[d]; k++ {
				similarity := fuzzySimilarity(terms[t], docTokens[d][k])
				if similarity >= matchThreshold {
					frequency += similarity
				}
			}
			tf[d*nterms+t] = frequency
			if frequency > 0.0 {
				df[t]++
			}
		}
	}
	for d := 0; d < ndocs; d++ {
		score := 0.0
		norm := bm25K1 * (1.0 - bm25B + bm25B*float64(docLengths[d])/averageLength)
		for t := 0; t < nterms; t++ {
			frequency := tf[d*nterms+t]
			if frequency <= 0.0 {
				continue
			}
			idf := math.Log(1.0 + (float64(ndocs-df[t])+0.5)/(float64(df[t])+0.5))
			score += idf * frequency * (bm25K1 + 1.0) / (frequency + norm)
		}
		out[d] = score
	}
	return out
}

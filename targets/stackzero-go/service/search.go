package main

// Product search: SQL candidate retrieval, BM25 ranking, rating enrichment.

import (
	"context"
	"sort"
)

type candidate struct {
	ID          int64
	Name        string
	Description string
	PriceCents  int64
	CategoryID  int64
}

type scoredCandidate struct {
	score     float64
	candidate candidate
}

type searchResult struct {
	ID         int64    `json:"id"`
	Name       string   `json:"name"`
	Price      string   `json:"price"`
	CategoryID int64    `json:"category_id"`
	Score      float64  `json:"score"`
	Reviews    int64    `json:"reviews"`
	Rating     *float64 `json:"rating"`
}

type searchResponse struct {
	Query   string         `json:"query"`
	Terms   []string       `json:"terms"`
	Results []searchResult `json:"results"`
}

// findCandidates returns products whose name or description contains any query term
// (first 300 per term).
func findCandidates(ctx context.Context, db Querier, terms []string) ([]candidate, error) {
	candidates := []candidate{}
	for _, term := range terms {
		pattern := "%" + term + "%"
		rows, err := fetch[candidate](ctx, db,
			"SELECT id, name, description, price_cents, category_id FROM products "+
				"WHERE name ILIKE $1 OR description ILIKE $2 ORDER BY id LIMIT 300",
			pattern, pattern)
		if err != nil {
			return nil, err
		}
		for _, row := range rows {
			seen := false
			for _, c := range candidates {
				if c == row {
					seen = true
					break
				}
			}
			if !seen {
				candidates = append(candidates, row)
			}
		}
	}
	return candidates, nil
}

func rank(terms []string, candidates []candidate, limit int) []scoredCandidate {
	docs := []string{}
	for _, c := range candidates {
		docs = append(docs, c.Name+" "+c.Description)
	}
	scores := scoreBatch(terms, docs)
	pairs := []scoredCandidate{}
	for i := range candidates {
		pairs = append(pairs, scoredCandidate{scores[i], candidates[i]})
	}
	sort.Slice(pairs, func(i, j int) bool {
		if pairs[i].score != pairs[j].score {
			return pairs[i].score > pairs[j].score
		}
		return pairs[i].candidate.ID < pairs[j].candidate.ID
	})
	return pairs[:min(limit, len(pairs))]
}

type ratingRow struct {
	Count   int64
	Average *float64
}

func ratingSummary(ctx context.Context, db Querier, productID int64) (int64, *float64, error) {
	row, _, err := fetchRow[ratingRow](ctx, db, "SELECT count(*), avg(rating)::float8 FROM reviews WHERE product_id = $1", productID)
	return row.Count, row.Average, err
}

func searchProducts(ctx context.Context, db Querier, query string, limit int64) (any, error) {
	terms := tokenize(query)
	if len(terms) == 0 {
		return searchResponse{Query: query, Terms: []string{}, Results: []searchResult{}}, nil
	}
	candidates, err := findCandidates(ctx, db, terms)
	if err != nil {
		return nil, err
	}
	ranked := rank(terms, candidates, int(limit))
	results := []searchResult{}
	for _, r := range ranked {
		count, average, err := ratingSummary(ctx, db, r.candidate.ID)
		if err != nil {
			return nil, err
		}
		var rating *float64
		if average != nil {
			v := pyRound(*average, 3)
			rating = &v
		}
		results = append(results, searchResult{
			ID:         r.candidate.ID,
			Name:       r.candidate.Name,
			Price:      formatMoney(r.candidate.PriceCents),
			CategoryID: r.candidate.CategoryID,
			Score:      pyRound(r.score, 6),
			Reviews:    count,
			Rating:     rating,
		})
	}
	return searchResponse{Query: query, Terms: terms, Results: results}, nil
}

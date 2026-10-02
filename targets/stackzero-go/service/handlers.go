package main

// HTTP endpoint handlers. Each takes the database handle plus parsed parameters and returns
// a JSON-serialisable value (or an *HTTPError).

import (
	"context"
	"sort"
	"strconv"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

type productRow struct {
	ID           int64
	Name         string
	Description  string
	PriceCents   int64
	CategoryID   int64
	CategoryName string
}

type reviewRow struct {
	ID         int64
	CustomerID int64
	Rating     int64
	Body       string
	CreatedAt  time.Time
}

type relatedRow struct {
	ID         int64
	Name       string
	PriceCents int64
}

type categoryRef struct {
	ID   int64  `json:"id"`
	Name string `json:"name"`
}

type ratingStats struct {
	Average   *float64         `json:"average"`
	Count     int              `json:"count"`
	Histogram map[string]int64 `json:"histogram"`
}

type recentReview struct {
	ID        int64   `json:"id"`
	Author    *string `json:"author"`
	Rating    int64   `json:"rating"`
	Body      string  `json:"body"`
	CreatedAt string  `json:"created_at"`
}

type relatedProduct struct {
	ID    int64  `json:"id"`
	Name  string `json:"name"`
	Price string `json:"price"`
}

type productDetailResponse struct {
	ID            int64            `json:"id"`
	Name          string           `json:"name"`
	Description   string           `json:"description"`
	Price         string           `json:"price"`
	Category      categoryRef      `json:"category"`
	Rating        ratingStats      `json:"rating"`
	RecentReviews []recentReview   `json:"recent_reviews"`
	Related       []relatedProduct `json:"related"`
}

func productDetail(ctx context.Context, db Querier, productID int64) (any, error) {
	row, found, err := fetchRow[productRow](ctx, db,
		"SELECT p.id, p.name, p.description, p.price_cents, p.category_id, c.name "+
			"FROM products p JOIN categories c ON c.id = p.category_id WHERE p.id = $1",
		productID)
	if err != nil {
		return nil, err
	}
	if !found {
		return nil, httpError(404, "product not found")
	}
	reviews, err := fetch[reviewRow](ctx, db,
		"SELECT id, customer_id, rating, body, created_at FROM reviews WHERE product_id = $1 ORDER BY created_at DESC, id DESC",
		productID)
	if err != nil {
		return nil, err
	}
	histogram := map[string]int64{"1": 0, "2": 0, "3": 0, "4": 0, "5": 0}
	for _, review := range reviews {
		histogram[strconv.FormatInt(review.Rating, 10)]++
	}
	var average *float64
	if len(reviews) > 0 {
		var total int64
		for _, review := range reviews {
			total += review.Rating
		}
		avg := pyRound(float64(total)/float64(len(reviews)), 3)
		average = &avg
	}
	recent := []recentReview{}
	for _, review := range reviews[:min(5, len(reviews))] {
		author, _, err := fetchVal[*string](ctx, db, "SELECT name FROM customers WHERE id = $1", review.CustomerID)
		if err != nil {
			return nil, err
		}
		recent = append(recent, recentReview{
			ID: review.ID, Author: author, Rating: review.Rating, Body: review.Body, CreatedAt: isoformat(review.CreatedAt),
		})
	}
	relatedRows, err := fetch[relatedRow](ctx, db,
		"SELECT id, name, price_cents FROM products WHERE category_id = $1 AND id <> $2 ORDER BY id LIMIT 5",
		row.CategoryID, row.ID)
	if err != nil {
		return nil, err
	}
	related := []relatedProduct{}
	for _, r := range relatedRows {
		related = append(related, relatedProduct{ID: r.ID, Name: r.Name, Price: formatMoney(r.PriceCents)})
	}
	return productDetailResponse{
		ID:            row.ID,
		Name:          row.Name,
		Description:   row.Description,
		Price:         formatMoney(row.PriceCents),
		Category:      categoryRef{ID: row.CategoryID, Name: row.CategoryName},
		Rating:        ratingStats{Average: average, Count: len(reviews), Histogram: histogram},
		RecentReviews: recent,
		Related:       related,
	}, nil
}

type customerRow struct {
	ID      int64
	Name    string
	Email   string
	Country string
}

type orderRow struct {
	ID         int64
	Status     string
	PlacedAt   time.Time
	TotalCents int64
}

type itemRow struct {
	ProductID      int64
	Quantity       int64
	UnitPriceCents int64
}

type customerInfo struct {
	ID      int64  `json:"id"`
	Name    string `json:"name"`
	Email   string `json:"email"`
	Country string `json:"country"`
}

type orderSummary struct {
	ID       int64  `json:"id"`
	Status   string `json:"status"`
	PlacedAt string `json:"placed_at"`
	Total    string `json:"total"`
	Items    int64  `json:"items"`
}

type favouriteProduct struct {
	ProductID int64   `json:"product_id"`
	Name      *string `json:"name"`
	Quantity  int64   `json:"quantity"`
}

type customerSummaryResponse struct {
	Customer          customerInfo       `json:"customer"`
	OrderCount        int                `json:"order_count"`
	TotalSpent        string             `json:"total_spent"`
	RecentOrders      []orderSummary     `json:"recent_orders"`
	FavouriteProducts []favouriteProduct `json:"favourite_products"`
}

func customerSummary(ctx context.Context, db Querier, customerID int64) (any, error) {
	customer, found, err := fetchRow[customerRow](ctx, db, "SELECT id, name, email, country FROM customers WHERE id = $1", customerID)
	if err != nil {
		return nil, err
	}
	if !found {
		return nil, httpError(404, "customer not found")
	}
	orders, err := fetch[orderRow](ctx, db,
		"SELECT id, status, placed_at, total_cents FROM orders WHERE customer_id = $1 ORDER BY placed_at DESC, id DESC",
		customerID)
	if err != nil {
		return nil, err
	}
	orderList := []orderSummary{}
	var totalSpent int64
	productQuantities := map[int64]int64{}
	for _, order := range orders {
		items, err := fetch[itemRow](ctx, db,
			"SELECT product_id, quantity, unit_price_cents FROM order_items WHERE order_id = $1 ORDER BY line_no",
			order.ID)
		if err != nil {
			return nil, err
		}
		var itemCount int64
		for _, item := range items {
			itemCount += item.Quantity
			productQuantities[item.ProductID] += item.Quantity
		}
		if order.Status != "returned" {
			totalSpent += order.TotalCents
		}
		orderList = append(orderList, orderSummary{
			ID: order.ID, Status: order.Status, PlacedAt: isoformat(order.PlacedAt), Total: formatMoney(order.TotalCents), Items: itemCount,
		})
	}
	type productQuantity struct{ productID, quantity int64 }
	favourites := make([]productQuantity, 0, len(productQuantities))
	for productID, quantity := range productQuantities {
		favourites = append(favourites, productQuantity{productID, quantity})
	}
	sort.Slice(favourites, func(i, j int) bool {
		if favourites[i].quantity != favourites[j].quantity {
			return favourites[i].quantity > favourites[j].quantity
		}
		return favourites[i].productID < favourites[j].productID
	})
	favouriteProducts := []favouriteProduct{}
	for _, f := range favourites[:min(5, len(favourites))] {
		name, _, err := fetchVal[*string](ctx, db, "SELECT name FROM products WHERE id = $1", f.productID)
		if err != nil {
			return nil, err
		}
		favouriteProducts = append(favouriteProducts, favouriteProduct{ProductID: f.productID, Name: name, Quantity: f.quantity})
	}
	return customerSummaryResponse{
		Customer:          customerInfo{ID: customer.ID, Name: customer.Name, Email: customer.Email, Country: customer.Country},
		OrderCount:        len(orderList),
		TotalSpent:        formatMoney(totalSpent),
		RecentOrders:      orderList[:min(10, len(orderList))],
		FavouriteProducts: favouriteProducts,
	}, nil
}

type topRow struct {
	ID      int64
	Name    string
	Units   int64
	Revenue int64
}

type topProduct struct {
	ID      int64  `json:"id"`
	Name    string `json:"name"`
	Units   int64  `json:"units"`
	Revenue string `json:"revenue"`
}

type categoryTopResponse struct {
	CategoryID int64        `json:"category_id"`
	Products   []topProduct `json:"products"`
}

func categoryTop(ctx context.Context, db Querier, categoryID int64, limit int64) (any, error) {
	exists, _, err := fetchVal[int64](ctx, db, "SELECT count(*) FROM categories WHERE id = $1", categoryID)
	if err != nil {
		return nil, err
	}
	if exists == 0 {
		return nil, httpError(404, "category not found")
	}
	rows, err := fetch[topRow](ctx, db,
		"SELECT p.id, p.name, sum(oi.quantity) AS units, sum(oi.quantity * oi.unit_price_cents) AS revenue "+
			"FROM order_items oi JOIN products p ON p.id = oi.product_id "+
			"WHERE p.category_id = $1 GROUP BY p.id, p.name ORDER BY units DESC, p.id LIMIT $2",
		categoryID, limit)
	if err != nil {
		return nil, err
	}
	products := []topProduct{}
	for _, row := range rows {
		products = append(products, topProduct{ID: row.ID, Name: row.Name, Units: row.Units, Revenue: formatMoney(row.Revenue)})
	}
	return categoryTopResponse{CategoryID: categoryID, Products: products}, nil
}

type suggestion struct {
	ProductID int64   `json:"product_id"`
	Name      *string `json:"name"`
	Score     int64   `json:"score"`
}

type recommendationsResponse struct {
	CustomerID      int64        `json:"customer_id"`
	BasedOn         []int64      `json:"based_on"`
	Recommendations []suggestion `json:"recommendations"`
}

func containsID(ids []int64, id int64) bool {
	for _, x := range ids {
		if x == id {
			return true
		}
	}
	return false
}

func recommendations(ctx context.Context, db Querier, customerID int64) (any, error) {
	exists, _, err := fetchVal[int64](ctx, db, "SELECT count(*) FROM customers WHERE id = $1", customerID)
	if err != nil {
		return nil, err
	}
	if exists == 0 {
		return nil, httpError(404, "customer not found")
	}
	rows, err := fetchColumn[int64](ctx, db,
		"SELECT oi.product_id FROM orders o JOIN order_items oi ON oi.order_id = o.id "+
			"WHERE o.customer_id = $1 ORDER BY o.placed_at DESC, o.id DESC, oi.line_no",
		customerID)
	if err != nil {
		return nil, err
	}
	bought := []int64{}
	for _, productID := range rows {
		if !containsID(bought, productID) {
			bought = append(bought, productID)
		}
	}
	counts := map[int64]int64{}
	for _, productID := range bought[:min(3, len(bought))] {
		coRows, err := fetchColumn[int64](ctx, db,
			"SELECT other.product_id FROM order_items mine JOIN order_items other "+
				"ON other.order_id = mine.order_id AND other.product_id <> mine.product_id "+
				"WHERE mine.product_id = $1",
			productID)
		if err != nil {
			return nil, err
		}
		for _, other := range coRows {
			if containsID(bought, other) {
				continue
			}
			counts[other]++
		}
	}
	type scored struct{ productID, score int64 }
	ranked := make([]scored, 0, len(counts))
	for productID, score := range counts {
		ranked = append(ranked, scored{productID, score})
	}
	sort.Slice(ranked, func(i, j int) bool {
		if ranked[i].score != ranked[j].score {
			return ranked[i].score > ranked[j].score
		}
		return ranked[i].productID < ranked[j].productID
	})
	suggestions := []suggestion{}
	for _, r := range ranked[:min(10, len(ranked))] {
		name, _, err := fetchVal[*string](ctx, db, "SELECT name FROM products WHERE id = $1", r.productID)
		if err != nil {
			return nil, err
		}
		suggestions = append(suggestions, suggestion{ProductID: r.productID, Name: name, Score: r.score})
	}
	return recommendationsResponse{CustomerID: customerID, BasedOn: bought[:min(3, len(bought))], Recommendations: suggestions}, nil
}

type dailyRow struct {
	PlacedAt   time.Time
	TotalCents int64
	Status     string
}

type dayEntry struct {
	Date         string `json:"date"`
	Orders       int64  `json:"orders"`
	Returned     int64  `json:"returned"`
	revenueCents int64
	Revenue      string `json:"revenue"`
}

type dailyReportResponse struct {
	AsOf  string     `json:"as_of"`
	Days  int64      `json:"days"`
	Daily []dayEntry `json:"daily"`
}

func dailyReport(ctx context.Context, db Querier, asOf time.Time, days int64) (any, error) {
	end := time.Date(asOf.Year(), asOf.Month(), asOf.Day()+1, 0, 0, 0, 0, time.UTC)
	start := end.AddDate(0, 0, -int(days))
	rows, err := fetch[dailyRow](ctx, db,
		"SELECT placed_at, total_cents, status FROM orders WHERE placed_at >= $1 AND placed_at < $2 ORDER BY placed_at, id",
		start, end)
	if err != nil {
		return nil, err
	}
	perDay := map[string]*dayEntry{}
	for _, row := range rows {
		day := row.PlacedAt.UTC().Format("2006-01-02")
		entry, ok := perDay[day]
		if !ok {
			entry = &dayEntry{Date: day}
			perDay[day] = entry
		}
		entry.Orders++
		if row.Status == "returned" {
			entry.Returned++
		} else {
			entry.revenueCents += row.TotalCents
		}
	}
	keys := make([]string, 0, len(perDay))
	for day := range perDay {
		keys = append(keys, day)
	}
	sort.Strings(keys)
	daysOut := []dayEntry{}
	for _, day := range keys {
		entry := perDay[day]
		entry.Revenue = formatMoney(entry.revenueCents)
		daysOut = append(daysOut, *entry)
	}
	return dailyReportResponse{AsOf: asOf.Format("2006-01-02"), Days: days, Daily: daysOut}, nil
}

type orderLine struct {
	productID int64
	quantity  int64
	price     int64
}

type stockRow struct {
	PriceCents int64
	Stock      int64
}

type createOrderResponse struct {
	OrderID    int64  `json:"order_id"`
	CustomerID int64  `json:"customer_id"`
	Total      string `json:"total"`
	Lines      int    `json:"lines"`
}

func createOrder(ctx context.Context, db *pgxpool.Pool, payload any) (any, error) {
	body, ok := payload.(map[string]any)
	if !ok {
		return nil, httpError(400, "body must be a JSON object")
	}
	customerID, err := parseInt(body["customer_id"], "customer_id", 1, 1_000_000_000)
	if err != nil {
		return nil, err
	}
	placedAt, err := parseTimestamp(body["placed_at"], "placed_at")
	if err != nil {
		return nil, err
	}
	items, ok := body["items"].([]any)
	if !ok || len(items) < 1 || len(items) > 10 {
		return nil, httpError(400, "items must be a list of 1..10 entries")
	}
	wanted := map[int64]int64{}
	for _, raw := range items {
		item, ok := raw.(map[string]any)
		if !ok {
			return nil, httpError(400, "each item must be an object")
		}
		productID, err := parseInt(item["product_id"], "product_id", 1, 1_000_000_000)
		if err != nil {
			return nil, err
		}
		quantity, err := parseInt(item["quantity"], "quantity", 1, 100)
		if err != nil {
			return nil, err
		}
		wanted[productID] += quantity
	}
	var orderID, total int64
	var lines []orderLine
	err = pgx.BeginFunc(ctx, db, func(tx pgx.Tx) error {
		_, found, err := fetchVal[int64](ctx, tx, "SELECT id FROM customers WHERE id = $1", customerID)
		if err != nil {
			return err
		}
		if !found {
			return httpError(404, "customer not found")
		}
		productIDs := make([]int64, 0, len(wanted))
		for productID := range wanted {
			productIDs = append(productIDs, productID)
		}
		// Lock product rows in id order so concurrent orders cannot deadlock.
		sort.Slice(productIDs, func(i, j int) bool { return productIDs[i] < productIDs[j] })
		for _, productID := range productIDs {
			product, found, err := fetchRow[stockRow](ctx, tx, "SELECT price_cents, stock FROM products WHERE id = $1 FOR UPDATE", productID)
			if err != nil {
				return err
			}
			if !found {
				return httpError(404, "product %d not found", productID)
			}
			if product.Stock < wanted[productID] {
				return httpError(409, "insufficient stock for product %d", productID)
			}
			total += product.PriceCents * wanted[productID]
			lines = append(lines, orderLine{productID, wanted[productID], product.PriceCents})
		}
		orderID, _, err = fetchVal[int64](ctx, tx,
			"INSERT INTO orders (customer_id, status, placed_at, total_cents) VALUES ($1, 'placed', $2, $3) RETURNING id",
			customerID, placedAt, total)
		if err != nil {
			return err
		}
		for lineNo, line := range lines {
			if _, err := tx.Exec(ctx,
				"INSERT INTO order_items (order_id, line_no, product_id, quantity, unit_price_cents) VALUES ($1, $2, $3, $4, $5)",
				orderID, lineNo+1, line.productID, line.quantity, line.price); err != nil {
				return err
			}
			if _, err := tx.Exec(ctx, "UPDATE products SET stock = stock - $1 WHERE id = $2", line.quantity, line.productID); err != nil {
				return err
			}
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	return createOrderResponse{OrderID: orderID, CustomerID: customerID, Total: formatMoney(total), Lines: len(lines)}, nil
}

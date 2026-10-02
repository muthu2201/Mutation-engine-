// Command shop serves the StackZero shop API on a Unix socket: the same HTTP contract,
// database schema and SQL as the Python reference implementation (targets/stackzero),
// written in Go. Routing, request parsing, error handling and lifecycle live here.
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"math"
	"net"
	"net/http"
	"os"
	"os/signal"
	"regexp"
	"runtime/debug"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

type route struct {
	method  string
	pattern *regexp.Regexp
	name    string
}

var routes = []route{
	{"GET", regexp.MustCompile(`^/healthz$`), "health"},
	{"GET", regexp.MustCompile(`^/products/search$`), "search"},
	{"GET", regexp.MustCompile(`^/products/(\d+)$`), "product"},
	{"GET", regexp.MustCompile(`^/customers/(\d+)/summary$`), "customer_summary"},
	{"GET", regexp.MustCompile(`^/customers/(\d+)/recommendations$`), "recommendations"},
	{"GET", regexp.MustCompile(`^/categories/(\d+)/top$`), "category_top"},
	{"GET", regexp.MustCompile(`^/reports/daily$`), "daily_report"},
	{"POST", regexp.MustCompile(`^/orders$`), "create_order"},
}

type app struct {
	db *pgxpool.Pool
}

// queryValues parses a query string like Python's urllib.parse.parse_qs: '&'-separated
// pairs, '+' is a space, invalid percent escapes are kept literally, and pairs with an
// empty value (or no '=') are dropped.
func queryValues(raw string) map[string][]string {
	out := map[string][]string{}
	for _, pair := range strings.Split(raw, "&") {
		name, value, ok := strings.Cut(pair, "=")
		if !ok || value == "" {
			continue
		}
		name, value = unquotePlus(name), unquotePlus(value)
		out[name] = append(out[name], value)
	}
	return out
}

func unquotePlus(s string) string {
	s = strings.ReplaceAll(s, "+", " ")
	if !strings.Contains(s, "%") {
		return s
	}
	var b strings.Builder
	for i := 0; i < len(s); i++ {
		if s[i] == '%' && i+2 < len(s) {
			if v, err := strconv.ParseUint(s[i+1:i+3], 16, 8); err == nil {
				b.WriteByte(byte(v))
				i += 2
				continue
			}
		}
		b.WriteByte(s[i])
	}
	return b.String()
}

func queryParam(query map[string][]string, name string) (string, bool) {
	values := query[name]
	if len(values) == 0 {
		return "", false
	}
	return values[0], true
}

func queryParamOr(query map[string][]string, name, def string) string {
	if v, ok := queryParam(query, name); ok {
		return v
	}
	return def
}

// pathID converts a matched path segment to an id. Ids beyond the integer column range
// cannot exist, so they map to -1, which no row has (the reference answers 404 for them).
func pathID(s string) int64 {
	n, err := strconv.ParseInt(s, 10, 64)
	if err != nil || n > math.MaxInt32 {
		return -1
	}
	return n
}

func (a *app) dispatch(ctx context.Context, name string, match []string, query map[string][]string, body []byte) (any, error) {
	switch name {
	case "health":
		return map[string]bool{"ok": true}, nil
	case "search":
		q := queryParamOr(query, "q", "")
		limit, err := parseInt(queryParamOr(query, "limit", "10"), "limit", 1, 50)
		if err != nil {
			return nil, err
		}
		return searchProducts(ctx, a.db, q, limit)
	case "product":
		return productDetail(ctx, a.db, pathID(match[1]))
	case "customer_summary":
		return customerSummary(ctx, a.db, pathID(match[1]))
	case "recommendations":
		return recommendations(ctx, a.db, pathID(match[1]))
	case "category_top":
		limit, err := parseInt(queryParamOr(query, "limit", "10"), "limit", 1, 100)
		if err != nil {
			return nil, err
		}
		return categoryTop(ctx, a.db, pathID(match[1]), limit)
	case "daily_report":
		asOfRaw, present := queryParam(query, "as_of")
		asOf, err := parseDate(asOfRaw, present, "as_of")
		if err != nil {
			return nil, err
		}
		days, err := parseInt(queryParamOr(query, "days", "7"), "days", 1, 366)
		if err != nil {
			return nil, err
		}
		return dailyReport(ctx, a.db, asOf, days)
	case "create_order":
		payload, err := decodeJSON(body)
		if err != nil {
			return nil, httpError(400, "invalid JSON body")
		}
		return createOrder(ctx, a.db, payload)
	}
	return nil, httpError(404, "not found")
}

// decodeJSON parses exactly one JSON value (an empty body is null), keeping numbers as
// json.Number so integers and floats keep Python's distinction.
func decodeJSON(body []byte) (any, error) {
	if len(body) == 0 {
		return nil, nil
	}
	dec := json.NewDecoder(bytes.NewReader(body))
	dec.UseNumber()
	var v any
	if err := dec.Decode(&v); err != nil {
		return nil, err
	}
	if _, err := dec.Token(); err != io.EOF {
		return nil, errors.New("trailing data after JSON value")
	}
	return v, nil
}

func respond(w http.ResponseWriter, status int, payload any) {
	body, err := json.Marshal(payload)
	if err != nil {
		log.Printf("encoding response: %v", err)
		status, body = 500, []byte(`{"error":"internal server error"}`)
	}
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Content-Length", strconv.Itoa(len(body)))
	w.WriteHeader(status)
	_, _ = w.Write(body)
}

func (a *app) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	var matched *route
	var match []string
	for i := range routes {
		m := routes[i].pattern.FindStringSubmatch(r.URL.Path)
		if m != nil && routes[i].method == r.Method {
			matched, match = &routes[i], m
			break
		}
	}
	if matched == nil {
		respond(w, 404, map[string]string{"error": "not found"})
		return
	}
	defer func() {
		if rec := recover(); rec != nil {
			log.Printf("panic serving %s %s: %v\n%s", r.Method, r.URL.Path, rec, debug.Stack())
			respond(w, 500, map[string]string{"error": "internal server error"})
		}
	}()
	var body []byte
	if r.Method == "POST" {
		body, _ = io.ReadAll(r.Body)
	}
	payload, err := a.dispatch(r.Context(), matched.name, match, queryValues(r.URL.RawQuery), body)
	if err != nil {
		var he *HTTPError
		if errors.As(err, &he) {
			respond(w, he.Status, map[string]string{"error": he.Message})
			return
		}
		log.Printf("error serving %s %s: %v", r.Method, r.URL.Path, err)
		respond(w, 500, map[string]string{"error": "internal server error"})
		return
	}
	status := 200
	if matched.name == "create_order" {
		status = 201
	}
	respond(w, status, payload)
}

func main() {
	uds := flag.String("uds", "", "Unix socket to listen on")
	flag.Parse()
	if *uds == "" {
		fmt.Fprintln(os.Stderr, "usage: shop --uds PATH")
		os.Exit(2)
	}
	ctx := context.Background()
	pool, err := openPool(ctx, loadSettings())
	if err != nil {
		log.Fatalf("database: %v", err)
	}
	defer pool.Close()
	ln, err := net.Listen("unix", *uds)
	if err != nil {
		log.Fatalf("listen: %v", err)
	}
	srv := &http.Server{Handler: &app{db: pool}, IdleTimeout: 30 * time.Second, ReadHeaderTimeout: 30 * time.Second}
	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGTERM, syscall.SIGINT)
	go func() {
		<-stop
		shutdown, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = srv.Shutdown(shutdown)
	}()
	log.Printf("shop listening on %s", *uds)
	if err := srv.Serve(ln); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatalf("serve: %v", err)
	}
}

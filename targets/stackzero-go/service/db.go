package main

import (
	"context"
	"errors"
	"fmt"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

// Querier is satisfied by the connection pool and by a transaction, so handlers run the
// same helpers inside and outside a transaction.
type Querier interface {
	Query(ctx context.Context, sql string, args ...any) (pgx.Rows, error)
	QueryRow(ctx context.Context, sql string, args ...any) pgx.Row
	Exec(ctx context.Context, sql string, args ...any) (pgconn.CommandTag, error)
}

// fetch runs a query and scans every row into a T by column position.
func fetch[T any](ctx context.Context, q Querier, sql string, args ...any) ([]T, error) {
	rows, err := q.Query(ctx, sql, args...)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, pgx.RowToStructByPos[T])
}

// fetchColumn runs a single-column query and returns the column's values.
func fetchColumn[T any](ctx context.Context, q Querier, sql string, args ...any) ([]T, error) {
	rows, err := q.Query(ctx, sql, args...)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, pgx.RowTo[T])
}

// fetchRow scans the first row into a T; found is false when the query returned no row.
func fetchRow[T any](ctx context.Context, q Querier, sql string, args ...any) (row T, found bool, err error) {
	rows, err := q.Query(ctx, sql, args...)
	if err != nil {
		return row, false, err
	}
	row, err = pgx.CollectOneRow(rows, pgx.RowToStructByPos[T])
	if errors.Is(err, pgx.ErrNoRows) {
		return row, false, nil
	}
	return row, err == nil, err
}

// fetchVal returns the first column of the first row; found is false when there is no row.
func fetchVal[T any](ctx context.Context, q Querier, sql string, args ...any) (val T, found bool, err error) {
	err = q.QueryRow(ctx, sql, args...).Scan(&val)
	if errors.Is(err, pgx.ErrNoRows) {
		return val, false, nil
	}
	return val, err == nil, err
}

var execModes = map[string]pgx.QueryExecMode{
	"cache_statement": pgx.QueryExecModeCacheStatement,
	"cache_describe":  pgx.QueryExecModeCacheDescribe,
	"describe_exec":   pgx.QueryExecModeDescribeExec,
	"exec":            pgx.QueryExecModeExec,
	"simple_protocol": pgx.QueryExecModeSimpleProtocol,
}

func openPool(ctx context.Context, s settings) (*pgxpool.Pool, error) {
	cfg, err := pgxpool.ParseConfig(s.DSN)
	if err != nil {
		return nil, err
	}
	cfg.MinConns = s.PoolMin
	cfg.MaxConns = s.PoolMax
	if s.PGOptions != "" {
		cfg.ConnConfig.RuntimeParams["options"] = s.PGOptions
	}
	mode, ok := execModes[s.ExecMode]
	if !ok {
		return nil, fmt.Errorf("unknown SHOP_EXEC_MODE %q", s.ExecMode)
	}
	cfg.ConnConfig.DefaultQueryExecMode = mode
	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		return nil, err
	}
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		return nil, err
	}
	return pool, nil
}

// Command gounits is Colloid's Go CodeRepresentation helper, built on the standard
// library's own Go parser (go/parser, go/ast, go/printer), so it can never disagree with
// the compiler about where a function starts and ends.
//
//	gounits units FILE           JSON list of the file's function and method units
//	gounits splice FILE NAME     read a function from stdin, print FILE with NAME replaced
//	gounits func NAME            read a code snippet from stdin, print the function NAME
//
// Units carry exact line spans, the source text, static call names, SQL found in string
// constants (including constant concatenations such as "SELECT ... " + "FROM ..."), the
// canonical signature and the offset of the body's opening brace (used by the causal
// profiler to inject a probe).
package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"go/ast"
	"go/parser"
	"go/printer"
	"go/token"
	"io"
	"os"
	"regexp"
	"sort"
	"strconv"
	"strings"
)

var sqlStart = regexp.MustCompile(`(?i)^\s*(SELECT|INSERT|UPDATE|DELETE|WITH)\b`)

type unit struct {
	Name       string   `json:"name"`
	Receiver   string   `json:"receiver"`
	StartLine  int      `json:"start_line"`
	EndLine    int      `json:"end_line"`
	Source     string   `json:"source"`
	Signature  string   `json:"signature"`
	Calls      []string `json:"calls"`
	SQL        []string `json:"sql"`
	BodyLbrace int      `json:"body_lbrace"` // byte offset of the body's '{' within Source
}

// receiverType renders a method receiver's base type name (pointer stars and type
// parameters dropped): func (s *Server) Run() -> "Server".
func receiverType(fd *ast.FuncDecl) string {
	if fd.Recv == nil || len(fd.Recv.List) == 0 {
		return ""
	}
	t := fd.Recv.List[0].Type
	for {
		switch x := t.(type) {
		case *ast.StarExpr:
			t = x.X
		case *ast.IndexExpr:
			t = x.X
		case *ast.IndexListExpr:
			t = x.X
		case *ast.Ident:
			return x.Name
		default:
			return ""
		}
	}
}

func qualifiedName(fd *ast.FuncDecl) string {
	if r := receiverType(fd); r != "" {
		return r + "." + fd.Name.Name
	}
	return fd.Name.Name
}

// signature is the canonical text of a function's receiver, name, type parameters,
// parameters and results (parameter names included: they are part of the locus contract).
func signature(fset *token.FileSet, fd *ast.FuncDecl) string {
	head := *fd
	head.Body = nil
	head.Doc = nil
	var b bytes.Buffer
	_ = (&printer.Config{Mode: printer.RawFormat}).Fprint(&b, fset, &head)
	return strings.Join(strings.Fields(b.String()), " ")
}

func callName(e ast.Expr) string {
	switch x := e.(type) {
	case *ast.Ident:
		return x.Name
	case *ast.SelectorExpr:
		if base := callName(x.X); base != "" {
			return base + "." + x.Sel.Name
		}
		return x.Sel.Name
	case *ast.IndexExpr: // generic instantiation: fetch[T](...)
		return callName(x.X)
	case *ast.IndexListExpr:
		return callName(x.X)
	}
	return ""
}

// constString evaluates a string constant expression made of literals joined with '+'.
func constString(e ast.Expr) (string, bool) {
	switch x := e.(type) {
	case *ast.BasicLit:
		if x.Kind != token.STRING {
			return "", false
		}
		s, err := strconv.Unquote(x.Value)
		return s, err == nil
	case *ast.BinaryExpr:
		if x.Op != token.ADD {
			return "", false
		}
		l, ok1 := constString(x.X)
		r, ok2 := constString(x.Y)
		return l + r, ok1 && ok2
	case *ast.ParenExpr:
		return constString(x.X)
	}
	return "", false
}

func analyse(fset *token.FileSet, src []byte, fd *ast.FuncDecl) unit {
	start := fset.Position(fd.Pos())
	end := fset.Position(fd.End())
	source := string(src[start.Offset:end.Offset])
	calls := map[string]bool{}
	var sql []string
	seenSQL := map[string]bool{}
	ast.Inspect(fd, func(n ast.Node) bool {
		switch x := n.(type) {
		case *ast.CallExpr:
			if name := callName(x.Fun); name != "" {
				calls[name] = true
			}
		case *ast.BinaryExpr, *ast.BasicLit:
			if s, ok := constString(x.(ast.Expr)); ok {
				if sqlStart.MatchString(s) {
					norm := strings.Join(strings.Fields(s), " ")
					if !seenSQL[norm] {
						seenSQL[norm] = true
						sql = append(sql, norm)
					}
				}
				return false // do not report the parts of a concatenation separately
			}
		}
		return true
	})
	names := make([]string, 0, len(calls))
	for c := range calls {
		names = append(names, c)
	}
	sort.Strings(names)
	lbrace := -1
	if fd.Body != nil {
		lbrace = fset.Position(fd.Body.Lbrace).Offset - start.Offset
	}
	return unit{
		Name: qualifiedName(fd), Receiver: receiverType(fd), StartLine: start.Line, EndLine: end.Line, Source: source + "\n",
		Signature: signature(fset, fd), Calls: names, SQL: sql, BodyLbrace: lbrace,
	}
}

func parseFile(name string, src []byte) (*token.FileSet, *ast.File, error) {
	fset := token.NewFileSet()
	f, err := parser.ParseFile(fset, name, src, parser.ParseComments|parser.SkipObjectResolution)
	return fset, f, err
}

func units(path string) ([]unit, error) {
	src, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	fset, f, err := parseFile(path, src)
	if err != nil {
		return nil, err
	}
	out := []unit{}
	for _, d := range f.Decls {
		if fd, ok := d.(*ast.FuncDecl); ok && fd.Body != nil {
			out = append(out, analyse(fset, src, fd))
		}
	}
	return out, nil
}

// parseSnippet parses a function given on its own (no package clause).
func parseSnippet(code string) (*token.FileSet, *ast.File, []byte, error) {
	src := []byte("package snippet\n\n" + code)
	fset, f, err := parseFile("snippet.go", src)
	return fset, f, src, err
}

func findFunc(f *ast.File, name string) []*ast.FuncDecl {
	var out []*ast.FuncDecl
	for _, d := range f.Decls {
		if fd, ok := d.(*ast.FuncDecl); ok && qualifiedName(fd) == name {
			out = append(out, fd)
		}
	}
	return out
}

func splice(path, name string, replacement []byte) ([]byte, error) {
	src, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	fset, f, err := parseFile(path, src)
	if err != nil {
		return nil, err
	}
	targets := findFunc(f, name)
	if len(targets) != 1 {
		return nil, fmt.Errorf("%s: %d functions named %s", path, len(targets), name)
	}
	_, rf, _, err := parseSnippet(string(replacement))
	if err != nil {
		return nil, fmt.Errorf("replacement does not parse: %v", err)
	}
	if len(rf.Decls) != 1 || len(findFunc(rf, name)) != 1 || len(rf.Imports) != 0 {
		return nil, fmt.Errorf("replacement must be exactly one function named %s", name)
	}
	start := fset.Position(targets[0].Pos()).Offset
	end := fset.Position(targets[0].End()).Offset
	out := append(append(append([]byte{}, src[:start]...), bytes.TrimRight(replacement, "\n")...), src[end:]...)
	fset2, f2, err := parseFile(path, out)
	if err != nil {
		return nil, fmt.Errorf("spliced file does not parse: %v", err)
	}
	if len(findFunc(f2, name)) != 1 {
		return nil, fmt.Errorf("spliced file does not define %s exactly once", name)
	}
	_ = fset2
	return out, nil
}

// funcFromSnippet returns the single top-level function NAME from arbitrary code (a model
// answer may include a package clause or imports; anything else at top level is an error).
func funcFromSnippet(code, name string) (map[string]any, error) {
	trimmed := strings.TrimSpace(code)
	var fset *token.FileSet
	var f *ast.File
	var src []byte
	var err error
	if strings.HasPrefix(trimmed, "package ") {
		src = []byte(code)
		fset, f, err = parseFile("snippet.go", src)
	} else {
		fset, f, src, err = parseSnippet(code)
	}
	if err != nil {
		return nil, fmt.Errorf("syntax error: %v", err)
	}
	matches := findFunc(f, name)
	if len(matches) != 1 {
		return nil, fmt.Errorf("response does not define %s exactly once", name)
	}
	extra := []string{}
	for _, d := range f.Decls {
		switch x := d.(type) {
		case *ast.FuncDecl:
			if x != matches[0] {
				extra = append(extra, "func "+qualifiedName(x))
			}
		case *ast.GenDecl:
			if x.Tok != token.IMPORT {
				extra = append(extra, x.Tok.String())
			}
		}
	}
	u := analyse(fset, src, matches[0])
	return map[string]any{"unit": u, "extra_decls": extra, "imports": len(f.Imports)}, nil
}

func fail(err error) {
	fmt.Fprintln(os.Stderr, err)
	os.Exit(1)
}

func main() {
	if len(os.Args) < 3 {
		fail(fmt.Errorf("usage: gounits units FILE | splice FILE NAME | func NAME"))
	}
	enc := json.NewEncoder(os.Stdout)
	switch os.Args[1] {
	case "units":
		us, err := units(os.Args[2])
		if err != nil {
			fail(err)
		}
		_ = enc.Encode(us)
	case "splice":
		if len(os.Args) != 4 {
			fail(fmt.Errorf("usage: gounits splice FILE NAME"))
		}
		repl, _ := io.ReadAll(os.Stdin)
		out, err := splice(os.Args[2], os.Args[3], repl)
		if err != nil {
			fail(err)
		}
		_, _ = os.Stdout.Write(out)
	case "func":
		code, _ := io.ReadAll(os.Stdin)
		res, err := funcFromSnippet(string(code), os.Args[2])
		if err != nil {
			fail(err)
		}
		_ = enc.Encode(res)
	default:
		fail(fmt.Errorf("unknown command %q", os.Args[1]))
	}
}

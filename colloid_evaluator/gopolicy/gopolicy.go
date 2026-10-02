// Command gopolicy is the evaluator's static policy scanner for Go genes (cascade L0). It is
// part of the judge: it lives in colloid_evaluator and is built from this file only, with
// the standard library's Go parser.
//
//	gopolicy PKGDIR FILE NAME < gene.json      {"source": ..., "baseline": ...}
//
// It prints {"reasons": [...], "strings": [...]}: the policy violations of the candidate
// function, and every string constant it contains (constant concatenations folded), which
// the language-neutral SQL policy then checks. Rules:
//
//   - confinement: exactly one function, the same qualified name and the same canonical
//     signature as the baseline; no imports, no other declarations, no compiler directives;
//   - no concurrency or timing: no go/select/send statements or channel types, no clock or
//     timer functions (time.Now, time.Sleep, time.After, ...), no output (fmt.Print...);
//   - no forbidden packages: unsafe, reflect, runtime, os, syscall, net, sync, log, plugin ...;
//   - no state that outlives a request: no assignment to package-level variables, no
//     mutation through parameters or the receiver (x.f = , x[i] = , *p = ), no taking the
//     address of package state, no delete/clear/copy/append/sort into it;
//   - no introspection of the connection pool or runtime (Stat, Config, Acquire, Hijack, ...):
//     a function that can see how loaded the system is can cheat only under load.
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
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
)

var forbiddenImports = []string{
	"unsafe", "reflect", "runtime", "os", "syscall", "net", "sync", "log", "plugin", "debug", "embed", "io/ioutil",
	"os/exec", "os/signal", "sync/atomic", "runtime/debug", "net/http", "log/slog", "internal",
}

var forbiddenMembers = map[string][]string{
	"time": {"Now", "Since", "Until", "Sleep", "After", "AfterFunc", "Tick", "NewTimer", "NewTicker"},
	"fmt":  {"Print", "Printf", "Println", "Fprint", "Fprintf", "Fprintln", "Scan", "Scanf", "Scanln", "Fscan", "Fscanf", "Fscanln"},
	"pgx":  {"Connect", "ConnectConfig", "ConnectWithOptions"},
}

// Selectors that expose pool/connection internals or process state, whatever the receiver.
var forbiddenSelectors = map[string]bool{
	"Stat": true, "Config": true, "Acquire": true, "AcquireFunc": true, "AcquireAllIdle": true, "Hijack": true,
	"PgConn": true, "Reset": true, "NumGoroutine": true, "SetFinalizer": true, "LockOSThread": true, "GOMAXPROCS": true,
	"AcquiredConns": true, "IdleConns": true, "TotalConns": true, "AcquireCount": true, "ConstructingConns": true,
}

var sortMutators = map[string]bool{
	"sort.Strings": true, "sort.Ints": true, "sort.Float64s": true, "sort.Slice": true, "sort.SliceStable": true, "sort.Sort": true,
	"sort.Stable": true, "slices.Sort": true, "slices.SortFunc": true, "slices.SortStableFunc": true, "slices.Reverse": true,
}

var forbiddenStrings = regexp.MustCompile(`(/opt/colloid|colloid_evaluator|/proc/|/sys/|/etc/|\.ssh|superuser\.pw)`)

type gene struct {
	Source   string `json:"source"`
	Baseline string `json:"baseline"`
}

type result struct {
	Reasons []string `json:"reasons"`
	Strings []string `json:"strings"`
}

func qualifiedName(fd *ast.FuncDecl) string {
	if fd.Recv != nil && len(fd.Recv.List) > 0 {
		t := fd.Recv.List[0].Type
		for {
			switch x := t.(type) {
			case *ast.StarExpr:
				t = x.X
				continue
			case *ast.IndexExpr:
				t = x.X
				continue
			case *ast.IndexListExpr:
				t = x.X
				continue
			case *ast.Ident:
				return x.Name + "." + fd.Name.Name
			}
			break
		}
	}
	return fd.Name.Name
}

func signature(fset *token.FileSet, fd *ast.FuncDecl) string {
	head := *fd
	head.Body = nil
	head.Doc = nil
	var b bytes.Buffer
	_ = (&printer.Config{Mode: printer.RawFormat}).Fprint(&b, fset, &head)
	return strings.Join(strings.Fields(b.String()), " ")
}

// fileImports maps the local name of each import of FILE to its path.
func fileImports(path string) (map[string]string, error) {
	fset := token.NewFileSet()
	f, err := parser.ParseFile(fset, path, nil, parser.ImportsOnly)
	if err != nil {
		return nil, err
	}
	out := map[string]string{}
	for _, imp := range f.Imports {
		p, _ := strconv.Unquote(imp.Path.Value)
		name := p[strings.LastIndex(p, "/")+1:]
		if strings.HasPrefix(name, "v") && len(name) <= 3 && strings.Count(p, "/") > 0 { // .../pgx/v5 -> pgx
			trimmed := strings.TrimSuffix(p, "/"+name)
			name = trimmed[strings.LastIndex(trimmed, "/")+1:]
		}
		if imp.Name != nil {
			name = imp.Name.Name
		}
		out[name] = p
	}
	return out, nil
}

// packageVars returns the names of the package-level variables of the package in DIR.
func packageVars(dir string) (map[string]bool, error) {
	files, err := filepath.Glob(filepath.Join(dir, "*.go"))
	if err != nil {
		return nil, err
	}
	out := map[string]bool{}
	for _, file := range files {
		if strings.HasSuffix(file, "_test.go") {
			continue
		}
		fset := token.NewFileSet()
		f, err := parser.ParseFile(fset, file, nil, parser.SkipObjectResolution)
		if err != nil {
			return nil, err
		}
		for _, d := range f.Decls {
			if gd, ok := d.(*ast.GenDecl); ok && gd.Tok == token.VAR {
				for _, spec := range gd.Specs {
					for _, n := range spec.(*ast.ValueSpec).Names {
						out[n.Name] = true
					}
				}
			}
		}
	}
	return out, nil
}

func constString(e ast.Expr) (string, bool) {
	switch x := e.(type) {
	case *ast.BasicLit:
		if x.Kind != token.STRING && x.Kind != token.CHAR {
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

type scanner struct {
	fn       *ast.FuncDecl
	params   map[*ast.Object]bool // parameters and receiver (results are local)
	imports  map[string]string
	pkgVars  map[string]bool
	reasons  map[string]bool
	strings  []string
	fset     *token.FileSet
	bodyOnly bool
}

func (s *scanner) add(format string, args ...any) { s.reasons[fmt.Sprintf(format, args...)] = true }

func root(e ast.Expr) *ast.Ident {
	for {
		switch x := e.(type) {
		case *ast.Ident:
			return x
		case *ast.SelectorExpr:
			e = x.X
		case *ast.IndexExpr:
			e = x.X
		case *ast.IndexListExpr:
			e = x.X
		case *ast.StarExpr:
			e = x.X
		case *ast.ParenExpr:
			e = x.X
		case *ast.SliceExpr:
			e = x.X
		default:
			return nil
		}
	}
}

// nonLocal reports whether an identifier names something not declared in this function:
// package-level state (object resolution leaves it unresolved) or an imported package.
func (s *scanner) nonLocal(id *ast.Ident) bool { return id.Obj == nil && id.Name != "_" }

func (s *scanner) isParam(id *ast.Ident) bool { return id.Obj != nil && s.params[id.Obj] }

func (s *scanner) text(e ast.Node) string {
	var b bytes.Buffer
	_ = printer.Fprint(&b, s.fset, e)
	return b.String()
}

// checkTarget enforces "no state that outlives a request" on an expression that is written to.
func (s *scanner) checkTarget(e ast.Expr, what string) {
	r := root(e)
	if r == nil {
		s.add("policy: %s through '%s' is not allowed", what, s.text(e))
		return
	}
	_, plain := e.(*ast.Ident)
	switch {
	case s.nonLocal(r):
		s.add("policy: %s package-level state '%s' is not allowed (state must not outlive a request)", what, s.text(e))
	case !plain && s.isParam(r):
		s.add("policy: %s '%s' mutates an argument - only objects created in this call may be mutated", what, s.text(e))
	}
}

func (s *scanner) selectorPackage(sel *ast.SelectorExpr) string {
	if id, ok := sel.X.(*ast.Ident); ok && s.nonLocal(id) {
		if _, isImport := s.imports[id.Name]; isImport {
			return id.Name
		}
	}
	return ""
}

func (s *scanner) visit(n ast.Node) bool {
	switch x := n.(type) {
	case *ast.GoStmt:
		s.add("policy: goroutines are not allowed (no background work; it would outlive the request)")
	case *ast.SelectStmt:
		s.add("policy: select statements are not allowed")
	case *ast.SendStmt:
		s.add("policy: channel sends are not allowed")
	case *ast.ChanType:
		s.add("policy: channels are not allowed")
	case *ast.AssignStmt:
		if x.Tok != token.DEFINE {
			for _, lhs := range x.Lhs {
				s.checkTarget(lhs, "assignment to")
			}
		}
	case *ast.IncDecStmt:
		s.checkTarget(x.X, "update of")
	case *ast.RangeStmt:
		if x.Tok == token.ASSIGN {
			for _, e := range []ast.Expr{x.Key, x.Value} {
				if e != nil {
					s.checkTarget(e, "assignment to")
				}
			}
		}
	case *ast.UnaryExpr:
		if x.Op == token.AND {
			if r := root(x.X); r != nil && s.nonLocal(r) && s.pkgVars[r.Name] {
				s.add("policy: taking the address of package-level state '%s' is not allowed", s.text(x.X))
			}
		}
	case *ast.CallExpr:
		name := ""
		switch f := x.Fun.(type) {
		case *ast.Ident:
			name = f.Name
		case *ast.SelectorExpr:
			if pkg := s.selectorPackage(f); pkg != "" {
				name = pkg + "." + f.Sel.Name
			}
		}
		if len(x.Args) > 0 && (name == "delete" || name == "clear" || name == "copy" || sortMutators[name]) {
			s.checkTarget(x.Args[0], "in-place change ("+name+") of")
		}
		if name == "append" && len(x.Args) > 0 {
			if r := root(x.Args[0]); r != nil && s.nonLocal(r) && s.pkgVars[r.Name] {
				s.add("policy: append to package-level state '%s' is not allowed", s.text(x.Args[0]))
			}
		}
	case *ast.SelectorExpr:
		if pkg := s.selectorPackage(x); pkg != "" {
			path := s.imports[pkg]
			for _, bad := range forbiddenImports {
				if path == bad || strings.HasPrefix(path, bad+"/") {
					s.add("policy: package '%s' is not allowed", path)
				}
			}
			for _, member := range forbiddenMembers[pkg] {
				if x.Sel.Name == member {
					s.add("policy: %s.%s is not allowed (no clocks, timers or output in a request path)", pkg, member)
				}
			}
		}
		if forbiddenSelectors[x.Sel.Name] {
			s.add("policy: '.%s' is not allowed (no introspection of the connection pool or the runtime)", x.Sel.Name)
		}
	case *ast.BasicLit, *ast.BinaryExpr:
		if str, ok := constString(x.(ast.Expr)); ok {
			s.strings = append(s.strings, str)
			if forbiddenStrings.MatchString(str) {
				s.add("policy: string constant references evaluator or system paths")
			}
			return false
		}
	}
	return true
}

func scan(pkgDir, file, name, source, baseline string) (result, error) {
	res := result{Reasons: []string{}, Strings: []string{}}
	imports, err := fileImports(file)
	if err != nil {
		return res, err
	}
	pkgVars, err := packageVars(pkgDir)
	if err != nil {
		return res, err
	}
	fset := token.NewFileSet()
	f, err := parser.ParseFile(fset, "gene.go", "package gene\n\n"+source, parser.ParseComments)
	if err != nil {
		res.Reasons = append(res.Reasons, fmt.Sprintf("syntax error: %v", err))
		return res, nil
	}
	bfset := token.NewFileSet()
	bf, err := parser.ParseFile(bfset, "base.go", "package gene\n\n"+baseline, parser.ParseComments)
	if err != nil || len(bf.Decls) != 1 {
		return res, fmt.Errorf("baseline does not parse as one function: %v", err)
	}
	base := bf.Decls[0].(*ast.FuncDecl)
	if len(f.Decls) != 1 || len(f.Imports) != 0 {
		res.Reasons = append(res.Reasons, "locus confinement: payload must contain exactly one function definition and nothing else")
		return res, nil
	}
	fn, ok := f.Decls[0].(*ast.FuncDecl)
	if !ok || fn.Body == nil {
		res.Reasons = append(res.Reasons, "locus confinement: payload must contain exactly one function definition and nothing else")
		return res, nil
	}
	if qualifiedName(fn) != name || qualifiedName(base) != name {
		res.Reasons = append(res.Reasons, fmt.Sprintf("locus confinement: function renamed %s -> %s", name, qualifiedName(fn)))
	}
	if signature(fset, fn) != signature(bfset, base) {
		res.Reasons = append(res.Reasons, "signature: receiver, parameters or results changed")
	}
	for _, cg := range f.Comments {
		for _, c := range cg.List {
			if strings.HasPrefix(c.Text, "//go:") || strings.HasPrefix(c.Text, "//line ") || strings.HasPrefix(c.Text, "/*line ") {
				res.Reasons = append(res.Reasons, "policy: compiler directives are not allowed in a function gene")
			}
		}
	}
	s := &scanner{fn: fn, params: map[*ast.Object]bool{}, imports: imports, pkgVars: pkgVars, reasons: map[string]bool{}, fset: fset}
	if fn.Recv != nil {
		for _, field := range fn.Recv.List {
			for _, n := range field.Names {
				if n.Obj != nil {
					s.params[n.Obj] = true
				}
			}
		}
	}
	for _, field := range fn.Type.Params.List {
		for _, n := range field.Names {
			if n.Obj != nil {
				s.params[n.Obj] = true
			}
		}
	}
	ast.Inspect(fn.Body, s.visit)
	for r := range s.reasons {
		res.Reasons = append(res.Reasons, r)
	}
	sort.Strings(res.Reasons)
	res.Strings = s.strings
	return res, nil
}

func main() {
	if len(os.Args) != 4 {
		fmt.Fprintln(os.Stderr, "usage: gopolicy PKGDIR FILE NAME < gene.json")
		os.Exit(2)
	}
	raw, _ := io.ReadAll(os.Stdin)
	var g gene
	if err := json.Unmarshal(raw, &g); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	res, err := scan(os.Args[1], os.Args[2], os.Args[3], g.Source, g.Baseline)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	_ = json.NewEncoder(os.Stdout).Encode(res)
}

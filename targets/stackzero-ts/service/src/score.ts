// Typo-tolerant BM25 ranking: the same algorithm, constants and floating-point operation order
// as the C library of the Python reference (targets/stackzero/native).

const MATCH_THRESHOLD = 0.75;
const BM25_K1 = 1.2;
const BM25_B = 0.75;

function isAlnum(c: number): boolean {
  return (c >= 97 && c <= 122) || (c >= 65 && c <= 90) || (c >= 48 && c <= 57);
}

// Lower-case ASCII alphanumeric tokens (any other character separates tokens).
export function shopTokenize(text: string): string[] {
  const tokens: string[] = [];
  let i = 0;
  while (i < text.length) {
    while (i < text.length && !isAlnum(text.charCodeAt(i))) i++;
    const start = i;
    while (i < text.length && isAlnum(text.charCodeAt(i))) i++;
    if (i > start) tokens.push(text.slice(start, i).toLowerCase());
  }
  return tokens;
}

export function levenshtein(a: string, b: string): number {
  const n = a.length, m = b.length;
  const d = new Array<number>((n + 1) * (m + 1)).fill(0);
  for (let i = 0; i <= n; i++) d[i * (m + 1)] = i;
  for (let j = 0; j <= m; j++) d[j] = j;
  for (let i = 1; i <= n; i++) {
    for (let j = 1; j <= m; j++) {
      const cost = a.charCodeAt(i - 1) === b.charCodeAt(j - 1) ? 0 : 1;
      const deletion = d[(i - 1) * (m + 1) + j] + 1;
      const insertion = d[i * (m + 1) + (j - 1)] + 1;
      const substitution = d[(i - 1) * (m + 1) + (j - 1)] + cost;
      const best = deletion < insertion ? deletion : insertion;
      d[i * (m + 1) + j] = best < substitution ? best : substitution;
    }
  }
  return d[n * (m + 1) + m];
}

export function fuzzySimilarity(a: string, b: string): number {
  const longest = Math.max(a.length, b.length);
  if (longest === 0) return 0.0;
  return 1.0 - levenshtein(a, b) / longest;
}

export function scoreBatch(terms: string[], docs: string[]): number[] {
  const ndocs = docs.length, nterms = terms.length;
  const out = new Array<number>(ndocs).fill(0);
  if (ndocs === 0) return out;
  const docTokens = docs.map(shopTokenize);
  const docLengths = docTokens.map((t) => t.length);
  const tf = new Array<number>(ndocs * Math.max(nterms, 1)).fill(0);
  const df = new Array<number>(Math.max(nterms, 1)).fill(0);
  let totalLength = 0.0;
  for (const len of docLengths) totalLength += len;
  let averageLength = totalLength / ndocs;
  if (averageLength <= 0.0) averageLength = 1.0;
  for (let d = 0; d < ndocs; d++) {
    for (let t = 0; t < nterms; t++) {
      let frequency = 0.0;
      for (let k = 0; k < docLengths[d]; k++) {
        const similarity = fuzzySimilarity(terms[t], docTokens[d][k]);
        if (similarity >= MATCH_THRESHOLD) frequency += similarity;
      }
      tf[d * nterms + t] = frequency;
      if (frequency > 0.0) df[t]++;
    }
  }
  for (let d = 0; d < ndocs; d++) {
    let score = 0.0;
    const norm = BM25_K1 * (1.0 - BM25_B + (BM25_B * docLengths[d]) / averageLength);
    for (let t = 0; t < nterms; t++) {
      const frequency = tf[d * nterms + t];
      if (frequency <= 0.0) continue;
      const idf = Math.log(1.0 + (ndocs - df[t] + 0.5) / (df[t] + 0.5));
      score += (idf * frequency * (BM25_K1 + 1.0)) / (frequency + norm);
    }
    out[d] = score;
  }
  return out;
}

/**
 * Market matching adapted from:
 * https://github.com/RichardFeynmanEnthusiast/kalshi-polymarket-market-matching
 *
 * Ports their preprocessing + feature scoring (clean text, Jaccard, numeric
 * values, key entities) into a lightweight Node matcher — without the heavy
 * BGE/FAISS/XGBoost stack, so it stays Coolify-friendly.
 */

const ENTITIES = {
  CPI: ['cpi', 'consumer price index'],
  FED: ['fed', 'federal reserve', 'fomc'],
  BTC: ['btc', 'bitcoin'],
  ETH: ['eth', 'ethereum'],
  GOP: ['gop', 'republican'],
  DNC: ['dnc', 'democrat', 'democratic'],
  SP500: ['sp500', 's&p 500', 's&p-500', 's and p'],
  TRUMP: ['trump'],
  HARRIS: ['harris'],
  BTC_HALVING: ['halving'],
};

/** Normalize market text the way the upstream preprocessor does. */
export function cleanText(text) {
  if (typeof text !== 'string') return '';
  return text
    .toLowerCase()
    .replace(/\[.*?\]/g, '')
    .replace(/https?:\/\/\S+|www\.\S+/g, '')
    .replace(/<.*?>+/g, '')
    .replace(/[^a-z0-9\s]/g, ' ')
    .replace(/\n/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

/** Extract first key numeric value ($1.5M, 45%, 500k, 3000). */
export function extractNumericalValue(text) {
  if (typeof text !== 'string') return null;
  const matches = text.toLowerCase().match(/\$?\s?[\d,]+(?:\.\d+)?\s?[kmb%]?/g);
  if (!matches || !matches.length) return null;
  let valStr = matches[0].replace(/\$/g, '').replace(/,/g, '').replace(/%/g, '').trim();
  let multiplier = 1;
  if (valStr.includes('k')) {
    multiplier = 1_000;
    valStr = valStr.replace('k', '');
  } else if (valStr.includes('m')) {
    multiplier = 1_000_000;
    valStr = valStr.replace('m', '');
  } else if (valStr.includes('b')) {
    multiplier = 1_000_000_000;
    valStr = valStr.replace('b', '');
  }
  const n = Number(valStr);
  return Number.isFinite(n) ? n * multiplier : null;
}

export function extractKeyEntities(text) {
  if (typeof text !== 'string') return [];
  const lower = text.toLowerCase();
  const found = new Set();
  for (const [entity, keywords] of Object.entries(ENTITIES)) {
    if (keywords.some((k) => lower.includes(k))) found.add(entity);
  }
  return [...found];
}

export function jaccardSimilarity(text1, text2) {
  const set1 = new Set(cleanText(text1).split(/\s+/).filter(Boolean));
  const set2 = new Set(cleanText(text2).split(/\s+/).filter(Boolean));
  if (!set1.size && !set2.size) return 0;
  let intersection = 0;
  for (const t of set1) if (set2.has(t)) intersection += 1;
  const union = set1.size + set2.size - intersection;
  return union === 0 ? 0 : intersection / union;
}

function levenshteinDistance(str1, str2) {
  const a = str1.toLowerCase();
  const b = str2.toLowerCase();
  const matrix = [];
  for (let i = 0; i <= b.length; i++) matrix[i] = [i];
  for (let j = 0; j <= a.length; j++) matrix[0][j] = j;
  for (let i = 1; i <= b.length; i++) {
    for (let j = 1; j <= a.length; j++) {
      if (b.charAt(i - 1) === a.charAt(j - 1)) matrix[i][j] = matrix[i - 1][j - 1];
      else matrix[i][j] = Math.min(matrix[i - 1][j - 1] + 1, matrix[i][j - 1] + 1, matrix[i - 1][j] + 1);
    }
  }
  return matrix[b.length][a.length];
}

function levenshteinSimilarity(str1, str2) {
  const maxLength = Math.max(str1.length, str2.length);
  if (maxLength === 0) return 1;
  return 1 - levenshteinDistance(str1, str2) / maxLength;
}

function numericSimilarity(a, b) {
  if (a == null || b == null) return 0.5; // neutral when missing
  if (a + b === 0) return 1;
  const pct = Math.abs(a - b) / ((Math.abs(a) + Math.abs(b)) / 2);
  if (pct <= 0.02) return 1;
  if (pct >= 1) return 0;
  return 1 - pct;
}

function entityOverlap(aList, bList) {
  if (!aList.length && !bList.length) return 0.5;
  if (!aList.length || !bList.length) return 0;
  const setB = new Set(bList);
  const hit = aList.filter((x) => setB.has(x)).length;
  const union = new Set([...aList, ...bList]).size;
  return union === 0 ? 0 : hit / union;
}

/**
 * Composite score inspired by upstream FEATURES:
 * jaccard + lexical + numeric agreement + shared entities.
 */
export function scorePair(polyText, kalshiText) {
  const a = cleanText(polyText);
  const b = cleanText(kalshiText);
  if (!a || !b) return 0;

  const jaccard = jaccardSimilarity(a, b);
  const leven = levenshteinSimilarity(a, b);
  const num = numericSimilarity(extractNumericalValue(polyText), extractNumericalValue(kalshiText));
  const entities = entityOverlap(extractKeyEntities(polyText), extractKeyEntities(kalshiText));

  // Weights favor lexical/semantic overlap; numeric+entity break ties / reject mismatches.
  let score = jaccard * 0.45 + leven * 0.25 + num * 0.15 + entities * 0.15;

  // Hard reject: both have numbers that differ a lot (e.g. $50k vs $100k).
  const nv = extractNumericalValue(polyText);
  const nk = extractNumericalValue(kalshiText);
  if (nv != null && nk != null) {
    const pct = Math.abs(nv - nk) / ((Math.abs(nv) + Math.abs(nk)) / 2 || 1);
    if (pct > 0.25) score *= 0.4;
  }

  // Boost exact entity alignment when present on both sides.
  if (entities >= 1) score = Math.min(1, score + 0.08);

  return score;
}

function matchText(outcome) {
  return (
    outcome.matchText
    || outcome.question
    || outcome.marketTitle
    || outcome.title
    || ''
  );
}

/**
 * Match Polymarket ↔ Kalshi outcomes.
 * Uses greedy best-score assignment after scoring all pairs above threshold
 * (better recall than one-pass poly→kalshi).
 */
export function matchOutcomes(polymarketOutcomes, kalshiOutcomes, threshold = 0.6) {
  const candidates = [];

  for (let i = 0; i < polymarketOutcomes.length; i++) {
    const poly = polymarketOutcomes[i];
    const polyText = matchText(poly);
    for (let j = 0; j < kalshiOutcomes.length; j++) {
      const kalshi = kalshiOutcomes[j];
      const score = scorePair(polyText, matchText(kalshi));
      if (score >= threshold) {
        candidates.push({ i, j, score, polymarket: poly, kalshi });
      }
    }
  }

  candidates.sort((a, b) => b.score - a.score);

  const usedPoly = new Set();
  const usedKalshi = new Set();
  const matches = [];

  for (const c of candidates) {
    if (usedPoly.has(c.i) || usedKalshi.has(c.j)) continue;
    usedPoly.add(c.i);
    usedKalshi.add(c.j);
    matches.push({
      polymarket: c.polymarket,
      kalshi: c.kalshi,
      similarity: c.score,
      source: 'rf-enhanced',
    });
  }

  return matches;
}

/** Backward-compatible export used by older tests. */
export function combinedSimilarity(str1, str2) {
  return scorePair(str1, str2);
}

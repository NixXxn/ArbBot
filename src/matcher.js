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
  DEM: ['democrat', 'democratic'],
  DNC_CHAIR: ['dnc chair', 'democratic national committee chair'],
  SP500: ['sp500', 's&p 500', 's&p-500', 's and p'],
  TRUMP: ['trump'],
  HARRIS: ['harris'],
  BTC_HALVING: ['halving'],
  PRESIDENT: ['president', 'presidential'],
  NOMINEE: ['nominee', 'nomination'],
  PRIME_MINISTER: ['prime minister'],
};

// If both sides name a country and they differ, reject.
const COUNTRIES = [
  'romania', 'israel', 'sweden', 'brazil', 'ukraine', 'russia', 'china', 'iran',
  'france', 'germany', 'united kingdom', 'uk', 'britain', 'england', 'canada', 'mexico', 'india',
  'japan', 'korea', 'taiwan', 'australia', 'italy', 'spain', 'poland', 'turkey',
  'argentina', 'venezuela', 'nigeria', 'egypt', 'saudi', 'usa', 'america', 'united states',
];

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

/** Years like 2026 / 2028 — mismatched years are usually different markets. */
export function extractYears(text) {
  if (typeof text !== 'string') return [];
  const years = text.match(/\b(20\d{2})\b/g) || [];
  return [...new Set(years.map(Number))];
}

/** Significant content tokens (drop stopwords). */
export function contentTokens(text) {
  const stop = new Set([
    'will', 'the', 'and', 'for', 'with', 'from', 'that', 'this', 'have', 'been',
    'were', 'their', 'about', 'after', 'before', 'would', 'could', 'should',
    'next', 'over', 'into', 'than', 'then', 'when', 'what', 'who', 'whom',
    'which', 'while', 'where', 'does', 'did', 'are', 'was', 'not', 'yes',
    'win', 'wins', 'won', 'election', 'nominee', 'party', 'control',
  ]);
  return cleanText(text)
    .split(/\s+/)
    .filter((w) => w.length > 3 && !stop.has(w) && !/^\d+$/.test(w));
}

function tokenOverlap(aText, bText) {
  const a = new Set(contentTokens(aText));
  const b = new Set(contentTokens(bText));
  if (!a.size || !b.size) return 0;
  let hit = 0;
  for (const t of a) if (b.has(t)) hit += 1;
  return hit / Math.min(a.size, b.size);
}

/**
 * Composite score inspired by upstream FEATURES:
 * jaccard + lexical + numeric agreement + shared entities + year gate.
 */
export function scorePair(polyText, kalshiText) {
  const a = cleanText(polyText);
  const b = cleanText(kalshiText);
  if (!a || !b) return 0;

  const yearsA = extractYears(polyText);
  const yearsB = extractYears(kalshiText);
  if (yearsA.length && yearsB.length) {
    const shareYear = yearsA.some((y) => yearsB.includes(y));
    if (!shareYear) return 0; // e.g. Fed 2026 vs Fed 2036
  }

  const countriesA = COUNTRIES.filter((c) => a.includes(c));
  const countriesB = COUNTRIES.filter((c) => b.includes(c));
  if (countriesA.length && countriesB.length && !countriesA.some((c) => countriesB.includes(c))) {
    return 0; // Sweden vs Romania, etc.
  }

  // Presidential nominee ≠ DNC chair (common false friend).
  const polyNom = /\bnominee\b|\bnomination\b|\bpresident/.test(a);
  const kalshiChair = /\bchair\b/.test(b);
  const polyChair = /\bchair\b/.test(a);
  const kalshiNom = /\bnominee\b|\bnomination\b|\bpresident/.test(b);
  if ((polyNom && kalshiChair) || (polyChair && kalshiNom)) return 0;

  // "Next PM / succeed" ≠ "which leader leaves office" (G7 exit vs election).
  const leaveOffice = (t) => /\bleave office\b|\bleaves office\b|\bleader out\b|\bg7 leader\b|\bwho will leave\b/.test(t);
  const nextOffice = (t) => /\bnext (new )?prime minister\b|\bsucceed\b|\bnext president\b|\bwin the .{0,40}nomination\b/.test(t);
  if ((leaveOffice(a) && nextOffice(b)) || (leaveOffice(b) && nextOffice(a))) return 0;

  const officesA = officeTags(polyText);
  const officesB = officeTags(kalshiText);
  if (officesA.length && officesB.length && !officesA.some((t) => officesB.includes(t))) {
    return 0; // Senate ≠ PM, Ballon d'Or ≠ DNC chair, ceasefire ≠ Riemann, …
  }

  const jaccard = jaccardSimilarity(a, b);
  const leven = levenshteinSimilarity(a, b);
  const num = numericSimilarity(extractNumericalValue(polyText), extractNumericalValue(kalshiText));
  const entities = entityOverlap(extractKeyEntities(polyText), extractKeyEntities(kalshiText));
  const tokens = tokenOverlap(polyText, kalshiText);

  // Weights favor lexical/semantic overlap; numeric+entity+tokens break ties.
  let score = jaccard * 0.35 + leven * 0.2 + num * 0.1 + entities * 0.15 + tokens * 0.2;

  // Hard reject: both have numbers that differ a lot (e.g. $50k vs $100k).
  const nv = extractNumericalValue(polyText);
  const nk = extractNumericalValue(kalshiText);
  if (nv != null && nk != null) {
    const pct = Math.abs(nv - nk) / ((Math.abs(nv) + Math.abs(nk)) / 2 || 1);
    if (pct > 0.25) score *= 0.4;
  }

  // Boost exact entity alignment when present on both sides.
  if (entities >= 1) score = Math.min(1, score + 0.08);
  if (tokens >= 0.5) score = Math.min(1, score + 0.05);

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

/** Named candidate / person from outcome label or "Will X be/win" phrasing. */
export function extractPersonName(outcomeOrText) {
  if (outcomeOrText && typeof outcomeOrText === 'object') {
    const cand = cleanText(outcomeOrText.candidate || '');
    if (
      cand
      && cand !== 'yes'
      && cand !== 'no'
      && !cand.startsWith('not ')
      && !cand.startsWith('::')
      && !/^[a-z]{1,5}$/.test(cand) // party codes like usr / pnl
    ) {
      return cand;
    }
    return extractPersonName(matchText(outcomeOrText));
  }
  const text = String(outcomeOrText || '');
  const m = text.match(/will\s+(.+?)\s+(?:be|win|become|get)\b/i);
  if (m) return cleanText(m[1]);
  // Kalshi sometimes appends the person after the question mark.
  if (text.includes('?')) {
    const tail = text.split('?').pop() || '';
    const cleaned = cleanText(tail);
    const words = cleaned.split(/\s+/).filter(Boolean);
    // Keep short name-like tails only (not a second full sentence).
    if (words.length >= 2 && words.length <= 5 && cleaned.length >= 6 && cleaned.length <= 60) {
      return cleaned;
    }
  }
  return '';
}

function personSimilarity(a, b) {
  if (!a || !b) return null;
  const ta = a.split(/\s+/).filter(Boolean);
  const tb = b.split(/\s+/).filter(Boolean);
  const lastA = ta[ta.length - 1];
  const lastB = tb[tb.length - 1];
  // Shared first name + different last name (Michael Olise vs Michael Blake) is not a match.
  if (lastA && lastB && lastA.length > 3 && lastB.length > 3 && lastA !== lastB) {
    return Math.min(jaccardSimilarity(a, b), 0.35);
  }
  const ja = jaccardSimilarity(a, b);
  const le = levenshteinSimilarity(a, b);
  const lastHit = lastA && lastB && lastA.length > 3 && lastA === lastB ? 0.92 : 0;
  return Math.max(ja, le, lastHit);
}

/** Coarse office / topic tags — mismatch → hard reject when both sides tagged. */
export function officeTags(text) {
  const t = cleanText(text);
  const tags = new Set();
  if (/\bsenate\b/.test(t)) tags.add('senate');
  if (/\bhouse\b|\bcongress\b/.test(t)) tags.add('house');
  if (/\bprime minister\b/.test(t)) tags.add('pm');
  if (/\bpresident/.test(t) && !/\bprime minister\b/.test(t)) tags.add('president');
  if (/\bchair\b/.test(t)) tags.add('chair');
  if (/\bballon\b|\bd or\b/.test(t)) tags.add('ballon');
  if (/\bceasefire\b|\bpeace (deal|agreement|treaty)\b/.test(t)) tags.add('ceasefire');
  if (/\briemann\b|\bhypothesis\b/.test(t)) tags.add('math');
  if (/\bfed\b|\bfomc\b|\brate cut\b/.test(t)) tags.add('fed');
  if (/\bbitcoin\b|\bbtc\b/.test(t)) tags.add('btc');
  if (/\bg7\b/.test(t)) tags.add('g7');
  return [...tags];
}

/**
 * Score two outcome objects (market text + person alignment).
 */
export function scoreOutcomes(poly, kalshi) {
  const polyText = matchText(poly);
  const kalshiText = matchText(kalshi);
  let score = scorePair(polyText, kalshiText);
  if (score <= 0) return 0;

  const pa = extractPersonName(poly);
  const pb = extractPersonName(kalshi);
  if (pa && pb) {
    const ps = personSimilarity(pa, pb);
    if (ps == null || ps < 0.45) return 0;
    score = Math.min(1, score * 0.45 + ps * 0.55);
  } else if (pa && !pb) {
    // Named Poly candidate vs party-only / unnamed Kalshi row — too ambiguous.
    const office = /\bprime minister\b|\bpresident\b|\bnominee\b|\bmayor\b|\bgovernor\b|\bleader\b/;
    if (office.test(cleanText(polyText)) || office.test(cleanText(kalshiText))) return 0;
    score *= 0.55;
  }

  return score;
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
    for (let j = 0; j < kalshiOutcomes.length; j++) {
      const kalshi = kalshiOutcomes[j];
      const score = scoreOutcomes(poly, kalshi);
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

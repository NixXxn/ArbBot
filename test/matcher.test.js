import {
  cleanText,
  extractNumericalValue,
  matchOutcomes,
  scorePair,
} from '../src/matcher.js';

describe('Similarity Matcher Logic', () => {
  test('should match exact strings', () => {
    const poly = [{ title: 'Donald Trump', marketId: 'p1' }];
    const kalshi = [{ title: 'Donald Trump', marketId: 'k1' }];
    const matches = matchOutcomes(poly, kalshi, 0.85);
    expect(matches.length).toBe(1);
    expect(matches[0].polymarket.title).toBe('Donald Trump');
  });

  test('should match case-insensitive', () => {
    const poly = [{ title: 'DONALD TRUMP', marketId: 'p1' }];
    const kalshi = [{ title: 'Donald Trump', marketId: 'k1' }];
    expect(matchOutcomes(poly, kalshi, 0.85).length).toBe(1);
  });

  test('should fuzzy match related names', () => {
    const poly = [{ title: 'Robert F. Kennedy Jr.', marketId: 'p1' }];
    const kalshi = [{ title: 'Robert Kennedy', marketId: 'k1' }];
    const matches = matchOutcomes(poly, kalshi, 0.5);
    expect(matches.length).toBe(1);
  });

  test('should NOT match unrelated names', () => {
    const poly = [{ title: 'Donald Trump', marketId: 'p1' }];
    const kalshi = [{ title: 'Joe Biden', marketId: 'k1' }];
    expect(matchOutcomes(poly, kalshi, 0.5).length).toBe(0);
  });

  test('should pick the BEST match when multiple candidates exist', () => {
    const poly = [{ title: 'Doug Burgum', marketId: 'p1' }];
    const kalshi = [
      { title: 'Doug Collins', marketId: 'k1' },
      { title: 'Doug Burgum', marketId: 'k2' },
    ];
    const matches = matchOutcomes(poly, kalshi, 0.5);
    expect(matches.length).toBe(1);
    expect(matches[0].kalshi.title).toBe('Doug Burgum');
  });

  test('should handle greedy matching (one-to-one)', () => {
    const poly = [
      { title: 'Name A', marketId: 'p1' },
      { title: 'Name B', marketId: 'p2' },
    ];
    const kalshi = [
      { title: 'Name A', marketId: 'k1' },
      { title: 'Name B', marketId: 'k2' },
    ];
    const matches = matchOutcomes(poly, kalshi, 0.8);
    expect(matches.length).toBe(2);
    expect(matches.find((m) => m.polymarket.marketId === 'p1').kalshi.marketId).toBe('k1');
  });

  test('cleanText strips noise like upstream preprocessor', () => {
    expect(cleanText('Will Bitcoin hit [$100k]? https://x.com')).toContain('bitcoin');
    expect(cleanText('Will Bitcoin hit [$100k]? https://x.com')).not.toContain('http');
  });

  test('numeric values reject mismatched strike prices', () => {
    const same = scorePair('Will BTC be above $100k?', 'Bitcoin above 100000');
    const diff = scorePair('Will BTC be above $50k?', 'Bitcoin above 100000');
    expect(same).toBeGreaterThan(diff);
    expect(extractNumericalValue('Will BTC be above $100k?')).toBe(100000);
  });

  test('matches market questions not just Yes/No labels', () => {
    const poly = [{ title: 'Yes', matchText: 'Fed cuts rates in October Yes', marketId: 'p1' }];
    const kalshi = [{ title: 'No', matchText: 'Will the Fed cut rates in October?', marketId: 'k1' }];
    expect(matchOutcomes(poly, kalshi, 0.45).length).toBe(1);
  });

  test('rejects year-mismatched markets', () => {
    expect(scorePair('How many Fed rate cuts in 2026?', 'Fed funds rate at end of 2036')).toBe(0);
    expect(scorePair(
      'Next Prime Minister of Romania? - Will Luca win?',
      'Who will be the next new Prime Minister of Romania?',
    )).toBeGreaterThan(0.45);
  });

  test('rejects country and office false friends', () => {
    expect(scorePair(
      'Next Prime Minister of Sweden - Will Ulf win?',
      'Who will be the next new Prime Minister of Romania?',
    )).toBe(0);
    expect(scorePair(
      'Democratic Presidential Nominee 2028 - Will Pete Buttigieg win?',
      'Who will be the next DNC Chair?',
    )).toBe(0);
    expect(scorePair(
      'Next Prime Minister of Sweden - Will Ulf Kristersson be the next Prime Minister of Sweden?',
      'Which G7 leader will leave office next? :: Prime Minister of the United Kingdom',
    )).toBe(0);
  });

  test('requires same person for named candidate markets', () => {
    const polySimion = {
      title: 'Next PM Romania',
      matchText: 'Next Prime Minister of Romania? - Will George Simion be the next Prime Minister of Romania?',
      candidate: 'George Simion',
      marketId: 'p1',
    };
    const kalshiUsr = {
      title: 'Romania PM',
      matchText: 'Who will be the next new Prime Minister of Romania? :: USR',
      candidate: ':: USR',
      marketId: 'k1',
    };
    const kalshiSimion = {
      title: 'Romania PM',
      matchText: 'Who will be the next new Prime Minister of Romania? George Simion',
      candidate: 'George Simion',
      marketId: 'k2',
    };
    expect(matchOutcomes([polySimion], [kalshiUsr], 0.4).length).toBe(0);
    expect(matchOutcomes([polySimion], [kalshiSimion], 0.4).length).toBe(1);
  });
});

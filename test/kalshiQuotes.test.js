import { enrichMatchKalshiQuotes, resolveKalshiTicker } from '../src/kalshiQuotes.js';

describe('kalshiQuotes', () => {
  test('resolveKalshiTicker strips YES/NO suffix', () => {
    expect(resolveKalshiTicker({ yesId: 'KXFOO-1-YES' })).toBe('KXFOO-1');
    expect(resolveKalshiTicker({ ticker: 'KXFOO-1' })).toBe('KXFOO-1');
  });

  test('enrichMatchKalshiQuotes applies public ask dollars as cents', async () => {
    const matches = [{
      kalshi: { marketId: 'KXFOO-1', yesPrice: 50, noPrice: 50 },
    }];
    const fetchFn = async () => ({
      ok: true,
      json: async () => ({
        market: {
          yes_ask_dollars: '0.19',
          no_ask_dollars: '0.86',
          volume_fp: '100',
          open_interest_fp: '50',
        },
      }),
    });
    const stats = await enrichMatchKalshiQuotes(matches, { fetchFn });
    expect(stats.enriched).toBe(1);
    expect(matches[0].kalshi.yesPrice).toBe(19);
    expect(matches[0].kalshi.noPrice).toBe(86);
    expect(matches[0].kalshi.quoteSource).toBe('kalshi-public');
  });
});

import { kalshiEventUrl, polymarketEventUrl, sizeTradeCents } from '../src/sizing.js';

describe('sizing', () => {
  test('CONSERVATIVE uses fixed cents capped by bankroll', () => {
    const r = sizeTradeCents({ mode: 'CONSERVATIVE', bankrollCents: 5000, fixedCents: 100 });
    expect(r.cents).toBe(100);
    const capped = sizeTradeCents({ mode: 'CONSERVATIVE', bankrollCents: 50, fixedCents: 100 });
    expect(capped.cents).toBe(50);
  });

  test('YOLO uses ~95% of bankroll', () => {
    const r = sizeTradeCents({ mode: 'YOLO', bankrollCents: 1000 });
    expect(r.cents).toBe(950);
  });

  test('KELLY scales up with edge and confidence', () => {
    const weak = sizeTradeCents({
      mode: 'KELLY',
      bankrollCents: 10_000,
      totalCostCents: 99,
      profitCents: 1,
      similarity: 0.55,
      kellyFraction: 0.5,
      kellyMaxBankrollPct: 0.25,
    });
    const strong = sizeTradeCents({
      mode: 'KELLY',
      bankrollCents: 10_000,
      totalCostCents: 90,
      profitCents: 10,
      similarity: 0.95,
      kellyFraction: 0.5,
      kellyMaxBankrollPct: 0.25,
    });
    expect(strong.cents).toBeGreaterThan(weak.cents);
    expect(strong.cents).toBeLessThanOrEqual(2500);
  });

  test('venue links prefer slug/ticker', () => {
    expect(polymarketEventUrl({ slug: 'fed-cut' })).toBe('https://polymarket.com/event/fed-cut');
    expect(kalshiEventUrl({ ticker: 'KXFED-1' })).toBe('https://kalshi.com/markets/kxfed-1');
  });
});

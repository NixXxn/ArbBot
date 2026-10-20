import { applyCashFlow, equalizeLedger, planRebalance } from '../src/rebalance.js';

describe('rebalance', () => {
  test('plans transfer from poly to kalshi when poly is heavy', () => {
    const plan = planRebalance(80, 20, { minDeltaUsd: 1 });
    expect(plan.from).toBe('polymarket');
    expect(plan.to).toBe('kalshi');
    expect(plan.transferUsd).toBe(30);
    expect(plan.equalized.polymarket).toBe(50);
    expect(plan.equalized.kalshi).toBe(50);
    expect(plan.tradeCapUsd).toBe(50);
  });

  test('plans transfer from kalshi to poly when kalshi is heavy', () => {
    const plan = planRebalance(10, 90, { minDeltaUsd: 1 });
    expect(plan.from).toBe('kalshi');
    expect(plan.to).toBe('polymarket');
    expect(plan.transferUsd).toBe(40);
  });

  test('ignores tiny imbalance under minDelta', () => {
    const plan = planRebalance(50.4, 49.6, { minDeltaUsd: 1 });
    expect(plan.balanced).toBe(true);
    expect(plan.transferUsd).toBe(0);
  });

  test('equalizeLedger redistributes after P&L skew', () => {
    // After a win, poly has the payout, kalshi was drained.
    const skewed = applyCashFlow({ polymarket: 40, kalshi: 40 }, 'polymarket', 20);
    const skewed2 = applyCashFlow(skewed, 'kalshi', -15);
    const { balances, plan } = equalizeLedger(skewed2);
    expect(balances.polymarket).toBe(plan.targetPoly);
    expect(balances.kalshi).toBe(plan.targetKalshi);
    expect(balances.polymarket + balances.kalshi).toBeCloseTo(85, 2);
  });
});

import pmxt from 'pmxtjs';
import { matchOutcomes } from './matcher.js';
import { findArbitrageOpportunities, getBestOpportunity } from './arbitrage.js';
import { enrichMatchKalshiQuotes } from './kalshiQuotes.js';
import {
  applyCashFlow,
  availableUsd,
  equalizeLedger,
  planRebalance,
} from './rebalance.js';
import { kalshiEventUrl, polymarketEventUrl, sizeTradeCents } from './sizing.js';
import { bumpStat, patchState, pushLog, setOpportunities } from './state.js';

function clientOpts(config) {
  const base = {};
  if (config.pmxtApiKey) base.pmxtApiKey = config.pmxtApiKey;
  return base;
}

export class ArbitrageBot {
  constructor(config) {
    this.config = config;
    this.currentPosition = null;
    this.pollInterval = null;
    const startEach = Math.max(1, Number(config.paperBudgetUsd || 1000) / 2);
    // Tradable budget ledger (equalized after each exit). Dry-run always uses this;
    // live seeds from fetchBalance when available.
    this.ledger = {
      polymarket: startEach,
      kalshi: startEach,
    };
    this.lastRebalance = null;

    const polyOpts = {
      ...clientOpts(config),
      ...(config.polymarketPrivateKey ? { privateKey: config.polymarketPrivateKey } : {}),
      ...(config.polymarketFunder ? { funderAddress: config.polymarketFunder, proxyAddress: config.polymarketFunder } : {}),
    };
    // signatureType: deposit wallet (3) ≈ gnosis-safe / poly-proxy depending on pmxt version
    if (config.polymarketSignatureType === 0) polyOpts.signatureType = 'eoa';
    else if (config.polymarketSignatureType === 1) polyOpts.signatureType = 'poly-proxy';
    else polyOpts.signatureType = 'gnosis-safe';

    const kalshiOpts = {
      ...clientOpts(config),
      ...(config.kalshiApiKey ? { apiKey: config.kalshiApiKey } : {}),
      ...(config.kalshiPrivateKey ? { privateKey: config.kalshiPrivateKey, apiSecret: config.kalshiPrivateKey } : {}),
    };

    const Polymarket = pmxt.Polymarket || pmxt.polymarket;
    const Kalshi = pmxt.Kalshi || pmxt.kalshi;
    this.polymarket = new Polymarket(polyOpts);
    this.kalshi = new Kalshi(kalshiOpts);
  }

  extractMarketId(url, platform) {
    if (platform === 'polymarket') {
      const match = url.match(/event\/([^/?]+)/);
      return match ? match[1] : null;
    }
    const parts = String(url).split('/').filter(Boolean);
    return parts[parts.length - 1].toUpperCase();
  }

  /** Dedupe by id/url and keep the richer row. */
  mergeMarkets(lists) {
    const byKey = new Map();
    for (const list of lists) {
      for (const m of list || []) {
        const key = String(m.id || m.marketId || m.url || m.title || Math.random());
        if (!byKey.has(key)) byKey.set(key, m);
      }
    }
    return [...byKey.values()];
  }

  async fetchVenueScan(client, limit) {
    const q = this.config.scanQuery || undefined;
    const reqs = [
      client.fetchMarkets({ limit, sort: 'volume', ...(q ? { query: q } : {}) }),
      client.fetchMarkets({ limit: Math.ceil(limit / 2), sort: 'liquidity', ...(q ? { query: q } : {}) }),
      client.fetchMarkets({ limit: Math.ceil(limit / 3), sort: 'newest', ...(q ? { query: q } : {}) }),
    ];
    const parts = await Promise.all(reqs.map((p) => p.catch(() => [])));
    return this.mergeMarkets(parts);
  }

  async fetchMarkets() {
    const scanAll = Boolean(this.config.scanAll);

    const fetchPoly = async () => {
      if (!scanAll) {
        const polymarketId = this.extractMarketId(this.config.polymarketUrl, 'polymarket');
        if (!polymarketId) return [];
        if (typeof this.polymarket.getMarketsBySlug === 'function') {
          return this.polymarket.getMarketsBySlug(polymarketId);
        }
        return this.polymarket.fetchMarkets({ slug: polymarketId, limit: 50 });
      }
      return this.fetchVenueScan(this.polymarket, this.config.scanLimitPoly || 300);
    };

    const fetchKalshi = async () => {
      if (!scanAll) {
        const kalshiId = this.extractMarketId(this.config.kalshiUrl, 'kalshi');
        if (!kalshiId) return [];
        if (typeof this.kalshi.getMarketsBySlug === 'function') {
          return this.kalshi.getMarketsBySlug(kalshiId);
        }
        return this.kalshi.fetchMarkets({ slug: kalshiId, limit: 50 });
      }
      return this.fetchVenueScan(this.kalshi, this.config.scanLimitKalshi || 300);
    };

    const [polymarketMarkets, kalshiMarkets] = await Promise.all([fetchPoly(), fetchKalshi()]);
    return {
      polymarketMarkets: Array.isArray(polymarketMarkets) ? polymarketMarkets : [],
      kalshiMarkets: Array.isArray(kalshiMarkets) ? kalshiMarkets : [],
      scanAll,
    };
  }

  parseOutcomes(markets, platform) {
    const minLiq = Number(this.config.minLiquidity || 0);
    const minVol = Number(this.config.minVolume || 0);
    return markets
      .filter((m) => m.outcomes && m.outcomes.length >= 2)
      .filter((m) => {
        // pmxt often returns 0 liquidity/volume for Kalshi — don't drop the whole venue.
        const liq = Number(m.liquidity) || 0;
        const vol = Number(m.volume) || Number(m.volume24h) || 0;
        if (platform === 'kalshi' && liq === 0 && vol === 0) return true;
        if (minLiq > 0 && liq < minLiq) return false;
        if (minVol > 0 && vol < minVol) return false;
        return true;
      })
      .map((market) => {
        // Binary markets are often "Candidate" / "Not Candidate" (not Yes/No).
        let yesOutcome = market.outcomes.find(
          (o) => {
            const l = (o.label || '').toLowerCase();
            return o.side === 'yes' || l === 'yes' || (l.startsWith('yes') && l.length < 8);
          },
        );
        let noOutcome = market.outcomes.find(
          (o) => {
            const l = (o.label || '').toLowerCase();
            return o.side === 'no' || l === 'no' || l.startsWith('not ');
          },
        );
        if (!yesOutcome || !noOutcome) {
          // Prefer the non-"Not …" label as the YES/named side.
          const named = market.outcomes.find((o) => !/^not\s/i.test(o.label || ''));
          const negated = market.outcomes.find((o) => /^not\s/i.test(o.label || ''));
          yesOutcome = yesOutcome || named || market.outcomes[0];
          noOutcome = noOutcome || negated || market.outcomes[1];
        }

        const marketTitle = market.question || market.title || market.name || market.slug || '';
        const outcomeLabel = yesOutcome?.label || '';
        const title = marketTitle || outcomeLabel || String(market.id || '');
        const yesId = yesOutcome.id || yesOutcome.outcomeId;
        const noId = noOutcome.id || noOutcome.outcomeId;
        const yesPrice = Number((Number(yesOutcome.price) * 100).toFixed(2));
        const noPrice = Number((Number(noOutcome.price) * 100).toFixed(2));

        const slug = market.slug || market.eventSlug || market.event_slug || null;
        const ticker = market.ticker || market.eventTicker || market.event_ticker || market.id || null;
        const eventUrl = market.url || market.eventUrl || market.marketUrl || null;

        return {
          title,
          marketTitle,
          question: marketTitle,
          candidate: outcomeLabel,
          matchText: `${marketTitle} ${outcomeLabel}`.trim(),
          marketId: market.id || market.marketId,
          slug,
          ticker,
          eventUrl,
          yesId,
          noId,
          yesPrice,
          noPrice,
          platform,
          volume: market.volume || market.volume24h || 0,
          liquidity: market.liquidity || 0,
          endDate: market.endDate || market.closeTime || market.close_time || market.resolutionDate || null,
        };
      });
  }

  /** Reset dry-run (or tracked) budget from the dashboard. */
  resetBudget(totalUsd, { clearPosition = true } = {}) {
    const total = Math.max(2, Number(totalUsd) || this.config.paperBudgetUsd || 1000);
    const each = Math.round((total / 2) * 100) / 100;
    this.ledger = { polymarket: each, kalshi: each };
    if (clearPosition) this.currentPosition = null;
    this.config.paperBudgetUsd = total;
    const plan = planRebalance(each, each, {
      targetRatio: this.config.rebalanceTargetRatio,
      minDeltaUsd: this.config.rebalanceMinDeltaUsd,
    });
    this.publishBalances(plan, 'reset');
    patchState({ currentPosition: null, currentPnLCents: 0 });
    pushLog('info', `BUDGET RESET → $${each} Poly / $${each} Kalshi (total $${total})`);
    return { polymarket: each, kalshi: each, total };
  }

  setTradingMode(mode) {
    const allowed = new Set(['CONSERVATIVE', 'KELLY', 'YOLO']);
    const next = String(mode || '').toUpperCase();
    if (!allowed.has(next)) throw new Error('tradingMode must be CONSERVATIVE, KELLY, or YOLO');
    this.config.tradingMode = next;
    patchState({ tradingMode: next });
    pushLog('info', `Trading mode → ${next}`);
    return next;
  }

  async executeTrade(platform, marketId, outcomeId, side, contracts, priceCents = null) {
    if (this.config.dryRun) {
      pushLog('info', `[DRY RUN] ${platform} ${side.toUpperCase()} ${contracts} on ${marketId}`);
      if (priceCents != null) {
        const usd = (contracts * Number(priceCents)) / 100;
        const delta = side.toLowerCase() === 'buy' ? -usd : usd;
        this.ledger = applyCashFlow(this.ledger, platform, delta);
      }
      return { success: true, orderId: `dry-run-${Date.now()}` };
    }

    const client = platform === 'polymarket' ? this.polymarket : this.kalshi;
    try {
      const order = await client.createOrder({
        marketId,
        outcomeId,
        side: side.toLowerCase(),
        amount: contracts,
        type: 'market',
      });
      return { success: true, orderId: order?.id || order?.orderId || 'ok' };
    } catch (error) {
      pushLog('error', `${platform} order failed: ${error.message}`);
      return { success: false, error: error.message };
    }
  }

  /** Read venue balances (live) or return the dry-run ledger. */
  async refreshBalances() {
    if (this.config.dryRun) {
      const plan = planRebalance(this.ledger.polymarket, this.ledger.kalshi, {
        targetRatio: this.config.rebalanceTargetRatio,
        minDeltaUsd: this.config.rebalanceMinDeltaUsd,
      });
      this.publishBalances(plan, 'ledger');
      return plan;
    }

    let poly = this.ledger.polymarket;
    let kalshi = this.ledger.kalshi;
    try {
      const rows = await this.polymarket.fetchBalance();
      const v = availableUsd(rows);
      if (v != null) poly = v;
    } catch (err) {
      pushLog('error', `Poly balance: ${err.message}`);
    }
    try {
      const rows = await this.kalshi.fetchBalance();
      const v = availableUsd(rows);
      if (v != null) kalshi = v;
    } catch (err) {
      pushLog('error', `Kalshi balance: ${err.message}`);
    }
    this.ledger = { polymarket: poly, kalshi };
    const plan = planRebalance(poly, kalshi, {
      targetRatio: this.config.rebalanceTargetRatio,
      minDeltaUsd: this.config.rebalanceMinDeltaUsd,
    });
    this.publishBalances(plan, 'venue');
    return plan;
  }

  publishBalances(plan, source) {
    this.lastRebalance = { ...plan, source, at: Date.now() };
    patchState({
      balances: {
        polymarket: plan.poly,
        kalshi: plan.kalshi,
        total: plan.total,
        tradeCapUsd: plan.tradeCapUsd,
        source,
      },
      rebalance: {
        balanced: plan.balanced,
        from: plan.from,
        to: plan.to,
        transferUsd: plan.transferUsd,
        targetPoly: plan.targetPoly,
        targetKalshi: plan.targetKalshi,
        imbalanceUsd: plan.imbalanceUsd,
        note: plan.balanced
          ? 'Budgets aligned'
          : `Move ~$${plan.transferUsd} ${plan.from} → ${plan.to} (bank/USDC). Until then trades size to min(side)=$${plan.tradeCapUsd}.`,
      },
    });
  }

  /**
   * After win/loss: keep both venues funded for the next arb.
   * Dry-run: equalize the cash ledger 50/50.
   * Live: read real balances, size to min(side), and show the bank/USDC
   * transfer needed (venues cannot wire to each other via API).
   */
  async rebalanceAfterPnl(reason = 'exit') {
    if (!this.config.rebalanceEnabled) {
      await this.refreshBalances();
      return;
    }

    if (!this.config.dryRun) {
      await this.refreshBalances();
      const plan = planRebalance(this.ledger.polymarket, this.ledger.kalshi, {
        targetRatio: this.config.rebalanceTargetRatio,
        minDeltaUsd: this.config.rebalanceMinDeltaUsd,
      });
      bumpStat('rebalances');
      this.publishBalances(plan, 'venue');
      if (plan.balanced) {
        pushLog('info', `REBALANCE (${reason}): venues even · Poly $${plan.poly} / Kalshi $${plan.kalshi}`);
      } else {
        pushLog(
          'info',
          `REBALANCE (${reason}): Poly $${plan.poly} / Kalshi $${plan.kalshi} · move ~$${plan.transferUsd} ${plan.from}→${plan.to} · next trade capped at $${plan.tradeCapUsd}`,
        );
      }
      return;
    }

    const before = { ...this.ledger };
    const { balances, plan } = equalizeLedger(this.ledger, {
      targetRatio: this.config.rebalanceTargetRatio,
      minDeltaUsd: this.config.rebalanceMinDeltaUsd,
    });
    this.ledger = balances;
    bumpStat('rebalances');
    // After ledger equalize, report as balanced for sizing.
    this.publishBalances({
      ...plan,
      poly: balances.polymarket,
      kalshi: balances.kalshi,
      total: balances.polymarket + balances.kalshi,
      tradeCapUsd: Math.min(balances.polymarket, balances.kalshi),
      balanced: true,
      from: null,
      to: null,
      transferUsd: 0,
      imbalanceUsd: 0,
    }, 'ledger-equalized');
    pushLog(
      'info',
      `REBALANCE (${reason}): $${before.polymarket}/$${before.kalshi} → $${balances.polymarket}/$${balances.kalshi} (dry-run ledger 50/50)`,
    );
  }

  tradeBudgetCents(opportunity = null) {
    const capUsd = Math.min(this.ledger.polymarket, this.ledger.kalshi);
    const bankrollCents = Math.floor(capUsd * 100);
    const sized = sizeTradeCents({
      mode: this.config.tradingMode,
      bankrollCents,
      fixedCents: this.config.tradeAmountCents,
      profitCents: opportunity?.profit,
      totalCostCents: opportunity?.totalCost,
      similarity: opportunity?.similarity,
      kellyFraction: this.config.kellyFraction,
      kellyMaxBankrollPct: this.config.kellyMaxBankrollPct,
      kellyMinCents: this.config.kellyMinCents,
      baseFillProb: this.config.kellyBaseFillProb,
    });
    return sized;
  }

  async executeArbitrage(opportunity) {
    pushLog('info', `EXECUTE ${opportunity.outcome}: ${opportunity.description} | ${opportunity.profit.toFixed(2)}¢`);
    bumpStat('tradesAttempted');

    await this.refreshBalances();
    const sized = this.tradeBudgetCents(opportunity);
    const tradeAmountCents = sized.cents;
    if (tradeAmountCents < 10) {
      pushLog('error', `Skip: budget too low (${sized.mode}/${sized.reason}, cap ${tradeAmountCents}¢).`);
      return false;
    }
    pushLog(
      'info',
      `SIZE ${sized.mode}: ${tradeAmountCents}¢`
        + (sized.p != null ? ` · p=${(sized.p * 100).toFixed(1)}%` : '')
        + (sized.fraction != null ? ` · f=${(sized.fraction * 100).toFixed(1)}%` : ''),
    );

    const polyPrice = opportunity.polymarketSide === 'YES'
      ? opportunity.polymarketOutcome.yesPrice
      : opportunity.polymarketOutcome.noPrice;
    const kalshiPrice = opportunity.kalshiSide === 'YES'
      ? opportunity.kalshiOutcome.yesPrice
      : opportunity.kalshiOutcome.noPrice;

    const polyContracts = Math.max(1, Math.floor(tradeAmountCents / (polyPrice || 1)));
    const kalshiContracts = Math.max(1, Math.floor(tradeAmountCents / (kalshiPrice || 1)));
    const polyOutcomeId = opportunity.polymarketSide === 'YES'
      ? opportunity.polymarketOutcome.yesId
      : opportunity.polymarketOutcome.noId;
    const kalshiOutcomeId = opportunity.kalshiSide === 'YES'
      ? opportunity.kalshiOutcome.yesId
      : opportunity.kalshiOutcome.noId;

    const [polyTrade, kalshiTrade] = await Promise.all([
      this.executeTrade('polymarket', opportunity.polymarketOutcome.marketId, polyOutcomeId, 'buy', polyContracts, polyPrice),
      this.executeTrade('kalshi', opportunity.kalshiOutcome.marketId, kalshiOutcomeId, 'buy', kalshiContracts, kalshiPrice),
    ]);

    if (polyTrade.success && kalshiTrade.success) {
      this.currentPosition = {
        opportunity,
        polymarketOrderId: polyTrade.orderId,
        kalshiOrderId: kalshiTrade.orderId,
        shares: { polymarket: polyContracts, kalshi: kalshiContracts },
        outcomeIds: { polymarket: polyOutcomeId, kalshi: kalshiOutcomeId },
        entryPrices: { polymarket: polyPrice, kalshi: kalshiPrice },
        entryTime: Date.now(),
      };
      bumpStat('tradesFilled');
      patchState({ currentPosition: this.currentPosition, balances: {
        polymarket: this.ledger.polymarket,
        kalshi: this.ledger.kalshi,
        total: this.ledger.polymarket + this.ledger.kalshi,
        tradeCapUsd: Math.min(this.ledger.polymarket, this.ledger.kalshi),
      }});
      return true;
    }
    pushLog('error', 'Trade execution failed (one or both legs)');
    return false;
  }

  calculateCurrentPnL(polymarketOutcomes, kalshiOutcomes) {
    if (!this.currentPosition) return 0;
    const polyNow = polymarketOutcomes.find(
      (o) => o.marketId === this.currentPosition.opportunity.polymarketOutcome.marketId,
    );
    const kalshiNow = kalshiOutcomes.find(
      (o) => o.marketId === this.currentPosition.opportunity.kalshiOutcome.marketId,
    );
    if (!polyNow || !kalshiNow) return 0;

    const polySide = this.currentPosition.opportunity.polymarketSide;
    const kalshiSide = this.currentPosition.opportunity.kalshiSide;
    const polyCurrentPrice = polySide === 'YES' ? polyNow.yesPrice : polyNow.noPrice;
    const kalshiCurrentPrice = kalshiSide === 'YES' ? kalshiNow.yesPrice : kalshiNow.noPrice;
    const polyValue = this.currentPosition.shares.polymarket * polyCurrentPrice;
    const kalshiValue = this.currentPosition.shares.kalshi * kalshiCurrentPrice;
    const polyCost = this.currentPosition.shares.polymarket * this.currentPosition.entryPrices.polymarket;
    const kalshiCost = this.currentPosition.shares.kalshi * this.currentPosition.entryPrices.kalshi;
    return (polyValue + kalshiValue) - (polyCost + kalshiCost);
  }

  shouldExitPosition(opportunities) {
    if (!this.currentPosition) return false;
    const currentOpp = opportunities.find((opp) => opp.outcome === this.currentPosition.opportunity.outcome);
    if (!currentOpp || currentOpp.profit < this.config.minProfitCents) return true;
    const bestOpp = getBestOpportunity(opportunities);
    if (bestOpp && bestOpp.profit > currentOpp.profit) {
      pushLog('info', `ROTATION → ${bestOpp.outcome} (${bestOpp.profit.toFixed(2)}¢)`);
      return true;
    }
    return false;
  }

  async exitPosition(markPrices = null) {
    if (!this.currentPosition) return;
    const heldTime = Math.round((Date.now() - this.currentPosition.entryTime) / 1000);
    pushLog('info', `EXITING ${this.currentPosition.opportunity.outcome} (held ${heldTime}s)`);
    const { outcomeIds, shares, opportunity, entryPrices } = this.currentPosition;
    const polyMark = markPrices?.polymarket ?? entryPrices.polymarket;
    const kalshiMark = markPrices?.kalshi ?? entryPrices.kalshi;
    await Promise.all([
      this.executeTrade('polymarket', opportunity.polymarketOutcome.marketId, outcomeIds.polymarket, 'sell', shares.polymarket, polyMark),
      this.executeTrade('kalshi', opportunity.kalshiOutcome.marketId, outcomeIds.kalshi, 'sell', shares.kalshi, kalshiMark),
    ]);
    bumpStat('exits');
    this.currentPosition = null;
    patchState({ currentPosition: null });
    // Always rebalance after realized win/loss so both sides keep budget.
    await this.rebalanceAfterPnl('exit');
  }

  async poll() {
    try {
      bumpStat('polls');
      const { polymarketMarkets, kalshiMarkets } = await this.fetchMarkets();
      const polymarketOutcomes = this.parseOutcomes(polymarketMarkets, 'polymarket');
      const kalshiOutcomes = this.parseOutcomes(kalshiMarkets, 'kalshi');
      const matches = matchOutcomes(polymarketOutcomes, kalshiOutcomes, this.config.matchingThreshold);
      // pmxt Kalshi books are often stub 50/50 — pull real asks from Kalshi public API.
      const quoteStats = await enrichMatchKalshiQuotes(matches);
      const allOpportunities = findArbitrageOpportunities(matches, this.config.minProfitCents)
        .filter((opp) => {
          const { yesPrice: polyYes, noPrice: polyNo } = opp.polymarketOutcome;
          const { yesPrice: kalshiYes, noPrice: kalshiNo, liquidity: kLiq } = opp.kalshiOutcome;
          // Stub books (exactly 50/50) are not tradable — common on thin Kalshi rows.
          const stub = (y, n) => Math.abs(y - 50) < 0.05 && Math.abs(n - 50) < 0.05;
          if (stub(polyYes, polyNo) || stub(kalshiYes, kalshiNo)) return false;
          // One-sided book (ask ~par) can't be used for cross-venue arb.
          if (kalshiYes >= 98 || kalshiNo >= 98) return false;
          if (polyYes >= 98 || polyNo >= 98) return false;
          const minKLiq = Number(this.config.minKalshiLiquidity || 0);
          if (minKLiq > 0 && (Number(kLiq) || 0) < minKLiq) return false;
          return polyYes > this.config.minPriceThreshold
            && polyNo > this.config.minPriceThreshold
            && kalshiYes > this.config.minPriceThreshold
            && kalshiNo > this.config.minPriceThreshold;
        })
        .map((opp) => ({
          ...opp,
          totalVolume: (opp.polymarketOutcome.volume || 0) + (opp.kalshiOutcome.volume || 0),
        }))
        .sort((a, b) => b.totalVolume - a.totalVolume);

      const topOpportunities = allOpportunities
        .slice(0, this.config.topNOpportunities)
        .sort((a, b) => b.profit - a.profit);

      const currentPnL = this.calculateCurrentPnL(polymarketOutcomes, kalshiOutcomes);
      setOpportunities(topOpportunities);
      patchState({
        lastPollAt: Date.now(),
        lastError: null,
        currentPnLCents: currentPnL,
        currentPosition: this.currentPosition,
        polyMarkets: polymarketMarkets.length,
        kalshiMarkets: kalshiMarkets.length,
        polyOutcomes: polymarketOutcomes.length,
        kalshiOutcomes: kalshiOutcomes.length,
        matches: matches.length,
        candidateCount: topOpportunities.length,
        kalshiQuotesEnriched: quoteStats.enriched,
      });

      pushLog(
        'info',
        `Scan poly=${polymarketMarkets.length} kalshi=${kalshiMarkets.length} → outcomes ${polymarketOutcomes.length}/${kalshiOutcomes.length} → matches ${matches.length} → kalshiQuotes ${quoteStats.enriched}/${quoteStats.tickers} → opps ${topOpportunities.length}`,
      );
      if (topOpportunities.length > 0) {
        pushLog(
          'info',
          `Top ${topOpportunities.length}: ${topOpportunities.map((o) => `${o.outcome} ${o.profit.toFixed(2)}¢`).join(' | ')}`,
        );
      } else {
        pushLog('info', `No arb ≥ ${this.config.minProfitCents}¢ (${matches.length} matches)`);
      }

      if (this.shouldExitPosition(topOpportunities)) {
        const pos = this.currentPosition;
        const polyNow = polymarketOutcomes.find((o) => o.marketId === pos?.opportunity?.polymarketOutcome?.marketId);
        const kalshiNow = kalshiOutcomes.find((o) => o.marketId === pos?.opportunity?.kalshiOutcome?.marketId);
        const polySide = pos?.opportunity?.polymarketSide;
        const kalshiSide = pos?.opportunity?.kalshiSide;
        await this.exitPosition({
          polymarket: polySide === 'YES' ? polyNow?.yesPrice : polyNow?.noPrice,
          kalshi: kalshiSide === 'YES' ? kalshiNow?.yesPrice : kalshiNow?.noPrice,
        });
      }
      if (!this.currentPosition && topOpportunities.length > 0) {
        await this.executeArbitrage(getBestOpportunity(topOpportunities));
      }
    } catch (error) {
      patchState({ lastError: error.message, lastPollAt: Date.now() });
      pushLog('error', error.message);
    }
  }

  async start() {
    patchState({
      running: true,
      dryRun: this.config.dryRun,
      liveKill: this.config.liveKill,
      tradingMode: this.config.tradingMode,
      polymarketUrl: this.config.polymarketUrl,
      kalshiUrl: this.config.kalshiUrl,
      scanAll: this.config.scanAll,
      pollIntervalSeconds: this.config.pollIntervalSeconds,
    });
    pushLog(
      'info',
      `Bot started | every ${this.config.pollIntervalSeconds}s | min ${this.config.minProfitCents}¢ | ${this.config.tradingMode} | dryRun=${this.config.dryRun} | rebalance=${this.config.rebalanceEnabled} | scanAll=${this.config.scanAll} | match≥${this.config.matchingThreshold}`,
    );
    await this.rebalanceAfterPnl('start');
    await this.poll();
    this.pollInterval = setInterval(async () => {
      await this.poll();
    }, this.config.pollIntervalSeconds * 1000);
  }

  stop() {
    if (this.pollInterval) clearInterval(this.pollInterval);
    this.pollInterval = null;
    patchState({ running: false });
    pushLog('info', 'Bot stopped');
  }
}

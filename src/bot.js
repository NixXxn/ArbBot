import pmxt from 'pmxtjs';
import { matchOutcomes } from './matcher.js';
import { findArbitrageOpportunities, getBestOpportunity } from './arbitrage.js';
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

  async fetchMarkets() {
    const polymarketId = this.extractMarketId(this.config.polymarketUrl, 'polymarket');
    const kalshiId = this.extractMarketId(this.config.kalshiUrl, 'kalshi');

    const fetchPoly = async () => {
      if (typeof this.polymarket.getMarketsBySlug === 'function') {
        return this.polymarket.getMarketsBySlug(polymarketId);
      }
      return this.polymarket.fetchMarkets({ slug: polymarketId, limit: 50 });
    };
    const fetchKalshi = async () => {
      if (typeof this.kalshi.getMarketsBySlug === 'function') {
        return this.kalshi.getMarketsBySlug(kalshiId);
      }
      return this.kalshi.fetchMarkets({ slug: kalshiId, limit: 50 });
    };

    const [polymarketMarkets, kalshiMarkets] = await Promise.all([fetchPoly(), fetchKalshi()]);
    return {
      polymarketMarkets: Array.isArray(polymarketMarkets) ? polymarketMarkets : [],
      kalshiMarkets: Array.isArray(kalshiMarkets) ? kalshiMarkets : [],
    };
  }

  parseOutcomes(markets, platform) {
    return markets
      .filter((m) => m.outcomes && m.outcomes.length >= 2)
      .map((market) => {
        const yesOutcome = market.outcomes.find(
          (o) => (o.label || '').toLowerCase().includes('yes') || o.side === 'yes',
        );
        const noOutcome = market.outcomes.find(
          (o) => (o.label || '').toLowerCase().includes('no') || o.side === 'no',
        );

        const title = yesOutcome ? yesOutcome.label : market.outcomes[0].label || market.title || market.id;
        const yesId = yesOutcome ? (yesOutcome.id || yesOutcome.outcomeId) : (market.outcomes[0].id || market.outcomes[0].outcomeId);
        const noId = noOutcome ? (noOutcome.id || noOutcome.outcomeId) : (market.outcomes[1].id || market.outcomes[1].outcomeId);

        const yesPrice = yesOutcome
          ? Number((yesOutcome.price * 100).toFixed(2))
          : Number((market.outcomes[0].price * 100).toFixed(2));
        const noPrice = noOutcome
          ? Number((noOutcome.price * 100).toFixed(2))
          : Number((market.outcomes[1].price * 100).toFixed(2));

        return {
          title,
          marketId: market.id || market.marketId,
          yesId,
          noId,
          yesPrice,
          noPrice,
          platform,
          volume: market.volume || 0,
        };
      });
  }

  async executeTrade(platform, marketId, outcomeId, side, contracts) {
    if (this.config.dryRun) {
      pushLog('info', `[DRY RUN] ${platform} ${side.toUpperCase()} ${contracts} on ${marketId}`);
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

  async executeArbitrage(opportunity) {
    pushLog('info', `EXECUTE ${opportunity.outcome}: ${opportunity.description} | ${opportunity.profit.toFixed(2)}¢`);
    bumpStat('tradesAttempted');

    const tradeAmountCents = this.config.tradingMode === 'YOLO' ? 1000 : this.config.tradeAmountCents;
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
      this.executeTrade('polymarket', opportunity.polymarketOutcome.marketId, polyOutcomeId, 'buy', polyContracts),
      this.executeTrade('kalshi', opportunity.kalshiOutcome.marketId, kalshiOutcomeId, 'buy', kalshiContracts),
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
      patchState({ currentPosition: this.currentPosition });
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

  async exitPosition() {
    if (!this.currentPosition) return;
    const heldTime = Math.round((Date.now() - this.currentPosition.entryTime) / 1000);
    pushLog('info', `EXITING ${this.currentPosition.opportunity.outcome} (held ${heldTime}s)`);
    const { outcomeIds, shares, opportunity } = this.currentPosition;
    await Promise.all([
      this.executeTrade('polymarket', opportunity.polymarketOutcome.marketId, outcomeIds.polymarket, 'sell', shares.polymarket),
      this.executeTrade('kalshi', opportunity.kalshiOutcome.marketId, outcomeIds.kalshi, 'sell', shares.kalshi),
    ]);
    bumpStat('exits');
    this.currentPosition = null;
    patchState({ currentPosition: null });
  }

  async poll() {
    try {
      bumpStat('polls');
      const { polymarketMarkets, kalshiMarkets } = await this.fetchMarkets();
      const polymarketOutcomes = this.parseOutcomes(polymarketMarkets, 'polymarket');
      const kalshiOutcomes = this.parseOutcomes(kalshiMarkets, 'kalshi');
      const matches = matchOutcomes(polymarketOutcomes, kalshiOutcomes, this.config.matchingThreshold);
      const allOpportunities = findArbitrageOpportunities(matches, this.config.minProfitCents)
        .filter((opp) => {
          const { yesPrice: polyYes, noPrice: polyNo } = opp.polymarketOutcome;
          const { yesPrice: kalshiYes, noPrice: kalshiNo } = opp.kalshiOutcome;
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
        polyOutcomes: polymarketOutcomes.length,
        kalshiOutcomes: kalshiOutcomes.length,
        matches: matches.length,
      });

      if (topOpportunities.length > 0) {
        pushLog(
          'info',
          `Top ${topOpportunities.length}: ${topOpportunities.map((o) => `${o.outcome} ${o.profit.toFixed(2)}¢`).join(' | ')}`,
        );
      } else {
        pushLog('info', `No arb ≥ ${this.config.minProfitCents}¢ (${matches.length} matches)`);
      }

      if (this.shouldExitPosition(topOpportunities)) await this.exitPosition();
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
      pollIntervalSeconds: this.config.pollIntervalSeconds,
    });
    pushLog(
      'info',
      `Bot started | every ${this.config.pollIntervalSeconds}s | min ${this.config.minProfitCents}¢ | ${this.config.tradingMode} | dryRun=${this.config.dryRun}`,
    );
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

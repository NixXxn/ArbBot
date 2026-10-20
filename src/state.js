/**
 * Shared in-memory bot status for the web dashboard.
 */
import fs from 'node:fs';
import path from 'node:path';

const MAX_LOGS = 200;

const state = {
  startedAt: Date.now(),
  updatedAt: null,
  running: false,
  dryRun: true,
  liveKill: true,
  tradingMode: 'CONSERVATIVE',
  polymarketUrl: '',
  kalshiUrl: '',
  pollIntervalSeconds: 60,
  lastError: null,
  lastPollAt: null,
  candidateCount: 0,
  opportunities: [],
  currentPosition: null,
  currentPnLCents: 0,
  stats: {
    polls: 0,
    tradesAttempted: 0,
    tradesFilled: 0,
    exits: 0,
  },
  logs: [],
};

let dataDir = 'data';

export function configureState({ dataDir: dir } = {}) {
  if (dir) dataDir = dir;
  fs.mkdirSync(dataDir, { recursive: true });
}

export function getState() {
  return {
    ...state,
    opportunities: state.opportunities.map(summarizeOpp),
    currentPosition: state.currentPosition
      ? {
          outcome: state.currentPosition.opportunity?.outcome,
          description: state.currentPosition.opportunity?.description,
          profit: state.currentPosition.opportunity?.profit,
          shares: state.currentPosition.shares,
          entryPrices: state.currentPosition.entryPrices,
          entryTime: state.currentPosition.entryTime,
          heldSeconds: Math.round((Date.now() - state.currentPosition.entryTime) / 1000),
        }
      : null,
  };
}

function summarizeOpp(opp) {
  return {
    outcome: opp.outcome,
    description: opp.description,
    profitCents: Number(opp.profit?.toFixed?.(2) ?? opp.profit),
    spreadPct: Number((((opp.profit || 0) / 100) * 100).toFixed(2)),
    totalCost: opp.totalCost,
    similarity: opp.similarity,
    totalVolume: opp.totalVolume || 0,
    polymarketSide: opp.polymarketSide,
    kalshiSide: opp.kalshiSide,
    polyYes: opp.polymarketOutcome?.yesPrice,
    polyNo: opp.polymarketOutcome?.noPrice,
    kalshiYes: opp.kalshiOutcome?.yesPrice,
    kalshiNo: opp.kalshiOutcome?.noPrice,
  };
}

export function patchState(partial) {
  Object.assign(state, partial, { updatedAt: Date.now() });
  persist();
}

export function setOpportunities(opps) {
  state.opportunities = opps || [];
  state.candidateCount = state.opportunities.length;
  state.updatedAt = Date.now();
  persist();
}

export function pushLog(level, message, extra = null) {
  const row = {
    at: new Date().toISOString(),
    level,
    message,
    ...(extra ? { extra } : {}),
  };
  state.logs.push(row);
  if (state.logs.length > MAX_LOGS) state.logs = state.logs.slice(-MAX_LOGS);
  state.updatedAt = Date.now();
  const line = `[${row.at}] ${level.toUpperCase()} ${message}`;
  if (level === 'error') console.error(line);
  else console.log(line);
  persist();
}

export function bumpStat(key, by = 1) {
  if (state.stats[key] == null) state.stats[key] = 0;
  state.stats[key] += by;
  persist();
}

function persist() {
  try {
    const file = path.join(dataDir, 'bot-status.json');
    fs.writeFileSync(file, JSON.stringify(getState(), null, 2) + '\n');
  } catch {
    // ignore disk errors in dashboard path
  }
}

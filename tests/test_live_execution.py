import asyncio
from decimal import Decimal
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as N
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"scripts"))
import live_executor as ex
from run_live import validate_signal


class ExecutionGuards(unittest.IsolatedAsyncioTestCase):
    def client(self):
        book=N(bids=[N(price=Decimal("0.19"),size=Decimal(100))],
            asks=[N(price=Decimal("0.2"),size=Decimal(100))], min_order_size=Decimal(5),tick_size=Decimal("0.01"))
        return N(get_order_book=AsyncMock(return_value=book),
            create_market_order=AsyncMock(return_value=N(maker_amount=1600000)),
            post_order=AsyncMock(return_value=N(ok=True)))

    async def test_budget_bounds_signed_amount(self):
        c=self.client()
        with patch.object(ex,"enabled_env"):
            await ex.buy(c,"unused","token",2,Decimal("0.21"))
        self.assertEqual(c.create_market_order.call_args.kwargs["amount"],"1.60")
        self.assertEqual(c.create_market_order.call_args.kwargs["order_type"],"FOK")
        c.post_order.assert_awaited_once()

    async def test_oversized_signature_never_posts(self):
        c=self.client();c.create_market_order.return_value=N(maker_amount=1600001)
        with patch.object(ex,"enabled_env"),self.assertRaises(ex.ExecutionStopped):
            await ex.buy(c,"unused","token",2,Decimal("0.21"))
        c.post_order.assert_not_awaited()

    async def test_stale_signal_never_posts(self):
        c=self.client()
        with patch.object(ex,"enabled_env"),patch.object(ex.time,"time",return_value=5),self.assertRaises(ex.ExecutionStopped):
            await ex.buy(c,"unused","token",2,Decimal("0.21"),signal_received_ms=1000)
        c.post_order.assert_not_awaited()

    async def test_budget_below_minimum_never_posts(self):
        c=self.client();c.get_order_book.return_value.asks[0].price=Decimal("0.6")
        c.get_order_book.return_value.bids[0].price=Decimal("0.59")
        with patch.object(ex,"enabled_env"),self.assertRaises(ex.ExecutionStopped):
            await ex.buy(c,"unused","token",2,Decimal("0.6"))
        c.post_order.assert_not_awaited()

    async def test_price_guard_is_explicitly_pre_submission(self):
        c=self.client();c.get_order_book.return_value.asks[0].price=Decimal("0.25")
        with patch.object(ex,"enabled_env"),self.assertRaises(ex.OrderNotSubmitted):
            await ex.buy(c,"unused","token",2,Decimal("0.21"))
        c.create_market_order.assert_not_awaited()
        c.post_order.assert_not_awaited()

    async def test_ambiguous_post_is_not_retried(self):
        c=self.client();c.post_order.side_effect=TimeoutError()
        with patch.object(ex,"enabled_env"),self.assertRaises(TimeoutError):
            await ex.buy(c,"unused","token",2,Decimal("0.21"))
        c.post_order.assert_awaited_once()

    async def test_sells_only_new_inventory(self):
        c=self.client();c.create_market_order.return_value=N()
        c.post_order.return_value=N(ok=False,code="unknown")
        with patch.object(ex,"token_balance",new=AsyncMock(return_value=Decimal(20))):
            await ex.sell_increment(c,"token",Decimal(12))
        self.assertEqual(c.create_market_order.call_args.kwargs["shares"],"8.00")
        c.post_order.assert_awaited_once()

    async def test_protected_position_exit_uses_fok_and_trigger_floor(self):
        c=self.client();c.get_order_book.return_value.bids[0].price=Decimal('.66')
        c.get_order_book.return_value.asks[0].price=Decimal('.67')
        c.create_market_order.return_value=N(maker_amount=5000000,taker_amount=3250000)
        with patch.object(ex,'enabled_env'):
            await ex.sell_position(c,'unused','down',5,Decimal('.65'))
        kwargs=c.create_market_order.call_args.kwargs
        self.assertEqual(kwargs['side'],'SELL');self.assertEqual(kwargs['shares'],'5.00')
        self.assertEqual(kwargs['min_price'],'0.65');self.assertEqual(kwargs['order_type'],'FOK')

    def test_kill_switch(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/".env";path.write_text("LIVE_KILL=1\n")
            with self.assertRaises(ex.ExecutionStopped):ex.enabled_env(path)

    def test_trading_wallet_omits_eoa_funder_for_deposit_wallet(self):
        from eth_account import Account
        key="0x"+("11"*32)
        signer=Account.from_key(key).address
        self.assertIsNone(ex.trading_wallet({"POLY_PRIVATE_KEY":key,"POLY_FUNDER":signer}))
        self.assertEqual(
            ex.trading_wallet({"POLY_PRIVATE_KEY":key,"POLY_FUNDER":"0x"+("22"*20)}),
            "0x"+"22"*20)

    def test_derive_creates_api_key_when_wallet_is_new(self):
        from eth_account import Account
        key="0x"+("11"*32)
        env={"POLY_PRIVATE_KEY":key,"POLY_FUNDER":Account.from_key(key).address,
             "POLY_SIGNATURE_TYPE":"3"}
        derive=type("R",(),{"status_code":400,"json":lambda self:{}})()
        create=type("R",(),{"status_code":200,
            "json":lambda self:{"apiKey":"k","secret":"s","passphrase":"p"}})()
        with patch.object(ex.requests,"get",return_value=derive),\
             patch.object(ex.requests,"post",return_value=create),\
             patch.object(ex.ApiKeyCreds,"model_validate",return_value="creds") as validate:
            self.assertEqual(ex.derive_existing(env),"creds")
            validate.assert_called_once_with({"apiKey":"k","secret":"s","passphrase":"p"})

    def test_signal_scope_and_age(self):
        good={"slug":"btc-updown-5m-100", "outcome":"Up","received_ms":1000,"end_ms":20000}
        validate_signal(good,1100)
        for changed in (dict(good,slug="weather"),dict(good,received_ms=0),dict(good,end_ms=2000)):
            with self.assertRaises(ex.ExecutionStopped):validate_signal(changed,1100)

    async def test_hash_without_trade_ids_confirms_receipt(self):
        response=N(ok=True,trade_ids=(),transactions_hashes=("0xtx",))
        client=N(wait_for_order_fill_settlement=AsyncMock(return_value=("0xtx",)))
        rpc=N(raise_for_status=lambda:None,json=lambda:{"result":{"status":"0x1"}})
        with patch.object(ex.requests,"post",return_value=rpc):
            self.assertEqual(await ex.settle(client,response),["0xtx"])

    async def test_reverted_transaction_is_not_success(self):
        response=N(ok=True,trade_ids=(),transactions_hashes=("0xtx",))
        client=N(wait_for_order_fill_settlement=AsyncMock(return_value=("0xtx",)))
        rpc=N(raise_for_status=lambda:None,json=lambda:{"result":{"status":"0x0"}})
        with patch.object(ex.requests,"post",return_value=rpc),self.assertRaises(ex.ExecutionStopped):
            await ex.settle(client,response)

    def test_durable_state_is_valid_json(self):
        import json
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"state.json"
            ex.save(path,{"buy_submission_intents":1})
            self.assertEqual(json.loads(path.read_text()),{"buy_submission_intents":1})


if __name__=="__main__":unittest.main()

import json
import math
import os
import random
import shutil
import time
import traceback

from cardano.wt.cardano_cli import CardanoCli
from cardano.wt.mint import Mint
from cardano.wt.utxo import Utxo

class BadUtxoError(ValueError):

    def __init__(self, utxo, message):
        super().__init__(message)
        self.utxo = utxo

class NftVendingMachine(object):

    __SINGLE_POLICY = 1
    __ERROR_WAIT = 30

    def as_json(self):
        return json.dumps(self, default=lambda o: o.__dict__, sort_keys=True, indent=4)

    def __init__(self, payment_addr, payment_sign_key, profit_addr, vend_randomly, single_vend_max, mint, blockfrost_api, cardano_cli, mainnet=False):
        self.payment_addr = payment_addr
        self.payment_sign_key = payment_sign_key
        self.profit_addr = profit_addr
        self.vend_randomly = vend_randomly
        self.single_vend_max = single_vend_max
        self.mint = mint
        self.blockfrost_api = blockfrost_api
        self.cardano_cli = cardano_cli
        self.mainnet = mainnet
        self.__is_validated = False

    def __get_tx_out_args(self, input_addr, change, nft_policy_map, total_profit, total_dev_fee):
        user_tokens = filter(None, [input_addr, str(change), CardanoCli.named_assets_str(nft_policy_map)])
        user_output = f"--tx-out \"{'+'.join(user_tokens)}\""
        profit_output = f"--tx-out \"{self.profit_addr}+{total_profit}\"" if total_profit else ''
        dev_output = f"--tx-out \"{self.mint.dev_addr}+{total_dev_fee}\"" if total_dev_fee else ''
        return [user_output, profit_output, dev_output]

    def __get_policy_name_map(self, metadata_file):
        nft_names = {}
        with open(metadata_file, 'r') as metadata_filehandle:
            cip25_metadata = json.load(metadata_filehandle)['721']
            for policy in cip25_metadata:
                if policy == 'version':
                    continue
                nft_names[policy] = list(cip25_metadata[policy].keys())
        return nft_names

    def __lock_and_merge(self, available_mints, num_mints, output_dir, locked_subdir, metadata_subdir, txn_id):
        combined_nft_metadata = {}
        for i in range(num_mints):
            mint_metadata_filename = available_mints.pop(0)
            mint_metadata_orig = os.path.join(self.mint.nfts_dir, mint_metadata_filename)
            with open(mint_metadata_orig, 'r') as mint_metadata_handle:
                mint_metadata = json.load(mint_metadata_handle)
                for policy in mint_metadata['721']:
                    if policy == 'version':
                        continue
                    for nft_name, nft_metadata in mint_metadata['721'][policy].items():
                        if not policy in combined_nft_metadata:
                            combined_nft_metadata[policy] = {}
                        combined_nft_metadata[policy][nft_name] = nft_metadata
            mint_metadata_locked = os.path.join(output_dir, locked_subdir, mint_metadata_filename)
            shutil.move(mint_metadata_orig, mint_metadata_locked)
        combined_output_path = os.path.join(output_dir, metadata_subdir, f"{txn_id}.json")
        with open(combined_output_path, 'w') as combined_metadata_handle:
            json.dump({'721': combined_nft_metadata }, combined_metadata_handle)
        return combined_output_path

    def __do_vend(self, mint_req, output_dir, locked_subdir, metadata_subdir):
        available_mints = sorted(os.listdir(self.mint.nfts_dir))
        if not available_mints:
            print("WARNING: Metadata directory is empty, please restock the vending machine...")
        elif self.vend_randomly:
            random.shuffle(available_mints)

        non_lovelace_bals = [balance for balance in mint_req.balances if balance.policy != Utxo.Balance.LOVELACE_POLICY]
        if non_lovelace_bals:
            raise BadUtxoError(mint_req, f"Cannot accept non-lovelace balances as payment")

        lovelace_bals = [balance for balance in mint_req.balances if balance.policy == Utxo.Balance.LOVELACE_POLICY]
        if len(lovelace_bals) != 1:
            raise BadUtxoError(mint_req, f"Found too many/few lovelace balances for UTXO {mint_req}")

        lovelace_bal = lovelace_bals.pop()
        num_mints_requested = math.floor(lovelace_bal.lovelace / self.mint.price) if self.mint.price else self.single_vend_max
        if not num_mints_requested:
            raise BadUtxoError(mint_req, f"User intentionally sent too little lovelace, avoiding txn processing to avoid DDoS")

        utxos = self.blockfrost_api.get_tx_utxos(mint_req.hash)
        utxo_inputs = utxos['inputs']
        utxo_outputs = utxos['outputs']
        input_addrs = set([utxo_input['address'] for utxo_input in utxo_inputs if not utxo_input['reference']])
        if len(input_addrs) < 1:
            raise BadUtxoError(mint_req, f"Txn hash {txn_hash} has no valid addresses ({utxo_inputs}), aborting...")
        input_addr = input_addrs.pop()

        wl_resources = self.mint.whitelist.required_info(mint_req, utxos, self.blockfrost_api)
        wl_availability = self.mint.whitelist.available(wl_resources)
        num_mints = min(self.single_vend_max, len(available_mints), num_mints_requested, wl_availability)

        if not self.mint.price and self.max_rebate > lovelace_bal.lovelace:
            print(f"Payment of {lovelace_bal.lovelace} might cause minUTxO error for {num_mints} NFTs, refunding instead...")
            num_mints = 0

        gross_profit = num_mints * self.mint.price
        change = lovelace_bal.lovelace - gross_profit

        if self.mint.bogo:
            bonuses = self.mint.bogo.determine_bonuses(num_mints_requested)
            print(f"Bonus of {bonuses} NFTs determined based on {num_mints_requested}")
            num_mints = min(self.single_vend_max, len(available_mints), (num_mints + bonuses))

        print(f"Beginning to mint {num_mints} NFTs to send to address {input_addr}")
        txn_id = int(time.time())
        nft_metadata_file = self.__lock_and_merge(available_mints, num_mints, output_dir, locked_subdir, metadata_subdir, txn_id)
        nft_policy_map = self.__get_policy_name_map(nft_metadata_file)

        all_names = [name for name_lst in nft_policy_map.values() for name in name_lst]
        total_name_chars = sum([len(name) for name in all_names])
        user_rebate = Mint.RebateCalculator.calculate_rebate_for(len(nft_policy_map.keys()), len(all_names), total_name_chars) if self.mint.price else 0
        dev_fee = num_mints * self.mint.dev_fee
        net_profit = gross_profit - dev_fee - user_rebate
        print(f"Minimum rebate to user is {user_rebate}, net profit to vault is {net_profit}")

        tx_ins = [f"--tx-in {mint_req.hash}#{mint_req.ix}"]
        tx_outs = self.__get_tx_out_args(input_addr, user_rebate + change, nft_policy_map, net_profit, dev_fee)
        mint_build_tmp = self.cardano_cli.build_raw_mint_txn(output_dir, txn_id, tx_ins, tx_outs, 0, nft_metadata_file, self.mint, nft_policy_map, self.script_map)

        tx_in_count = len(tx_ins)
        tx_out_count = len([tx_out for tx_out in tx_outs if tx_out])
        signers = [self.payment_sign_key]
        if num_mints:
            signers.extend(self.mint.sign_keys)
        fee = self.cardano_cli.calculate_min_fee(mint_build_tmp, tx_in_count, tx_out_count, len(signers))

        if net_profit:
            net_profit = net_profit - fee
        else:
            change = change - fee

        final_change = user_rebate + change
        if (final_change and (final_change < Utxo.MIN_UTXO_VALUE)) or (net_profit and (net_profit < Utxo.MIN_UTXO_VALUE)):
            raise BadUtxoError(mint_req, f"UTxO left change of {change}, and net_profit of {net_profit}, causing a minUTxO error")

        tx_outs = self.__get_tx_out_args(input_addr, final_change, nft_policy_map, net_profit, dev_fee)
        mint_build = self.cardano_cli.build_raw_mint_txn(output_dir, txn_id, tx_ins, tx_outs, fee, nft_metadata_file, self.mint, nft_policy_map, self.script_map)
        mint_signed = self.cardano_cli.sign_txn(signers, mint_build)
        self.mint.whitelist.consume(wl_resources, num_mints)
        self.blockfrost_api.submit_txn(mint_signed)

    def vend(self, output_dir, locked_subdir, metadata_subdir, exclusions):
        if not self.__is_validated:
            raise ValueError('Attempting to vend from non-validated vending machine')
        mint_reqs = self.blockfrost_api.get_utxos(self.payment_addr, exclusions)
        for mint_req in mint_reqs:
            exclusions.add(mint_req)
            try:
                self.__do_vend(mint_req, output_dir, locked_subdir, metadata_subdir)
            except BadUtxoError as e:
                print(f"UNRECOVERABLE UTXO ERROR\n{e.utxo}\n^--- REQUIRES INVESTIGATION")
                print(traceback.format_exc())
            except Exception as e:
                print(f"WARNING: Uncaught exception for {mint_req}, added to exclusions (RETRY WILL NOT BE ATTEMPTED)")
                print(traceback.format_exc())
                time.sleep(NftVendingMachine.__ERROR_WAIT)

    def validate(self):
        self.mint.validate()
        if self.payment_addr == self.profit_addr:
            raise ValueError(f"Payment address and profit address ({self.payment_addr}) cannot be the same!")
        self.max_rebate = self.__max_rebate_for(self.mint.validated_names)
        if self.mint.price and self.mint.price < (self.max_rebate + self.mint.dev_fee + Utxo.MIN_UTXO_VALUE):
            raise ValueError(f"Price of {self.mint.price} with dev fee of {self.mint.dev_fee} could lead to a minUTxO error due to rebates")
        if not os.path.exists(self.payment_sign_key):
            raise ValueError(f"Payment signing key file '{self.payment_sign_key}' not found on filesystem")
        expected_payment_addr = self.cardano_cli.build_addr(self.payment_sign_key, self.mainnet)
        if not expected_payment_addr == self.payment_addr:
            raise ValueError(f"Could not match {self.payment_addr} to signature at '{self.payment_sign_key}' (expected {expected_payment_addr})")
        self.script_map = {}
        for policy in self.mint.policies:
            self.script_map[policy] = self.__validate_script_file(policy)
            if not self.script_map[policy]:
                raise ValueError(f"No matching script file found for policy {policy}")
        self.__is_validated = True

    def __validate_script_file(self, policy):
        for script in self.mint.scripts:
            if self.cardano_cli.policy_id(script) == policy:
                return script
        return None

    def __max_rebate_for(self, nft_names):
        max_len = 0 if not nft_names else max([len(nft_name.split('.')[1]) for nft_name in nft_names])
        all_policies = [nft_name.split('.')[0] for nft_name in nft_names]
        return Mint.RebateCalculator.calculate_rebate_for(
            len(set(all_policies)),
            self.single_vend_max,
            max_len * self.single_vend_max
        )

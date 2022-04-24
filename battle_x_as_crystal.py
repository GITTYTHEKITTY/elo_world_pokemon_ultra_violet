import os
import random
import shutil

import constants.file_paths as files
from constants.memory import wEnemyTrainerItems
from utils.battle_logger import start_new_battle, end_battle, add_turn
from utils.bgb import call_bgb
from utils.data import *
from utils.demos import *
from utils.files import *
from utils.movies import build_movie, MovieContext


def load_trainer_info(trainer_class: int, trainer_id: int, in_save: bytearray, working_save: str) -> bytearray:
	save = in_save.copy()
	set_value(save, [trainer_class], memory.wOtherTrainerClass)
	set_value(save, [trainer_id], memory.wOtherTrainerID)
	write_file(working_save, save)

	call_bgb(in_save=working_save,
	         out_save=working_save,
	         breakpoint_list=['PlaceCommandCharacter'])

	return load_save(working_save)


def set_up_battle_save(base_save: bytearray, player_trainer_info: bytearray, enemy_class: int,
                       enemy_index: int, enemy_gender: str, rng: random.Random) -> bytearray:
	battle_save = base_save.copy()

	copy_values(player_trainer_info, memory.wOTParty, battle_save, memory.wPlayerParty)

	enemy_party_size = get_value(player_trainer_info, memory.wOTPartyCount)[0]

	for i in range(enemy_party_size):
		pokemon = get_value(player_trainer_info, memory.enemyParty[i])
		pokemon_index = pokemon[0] - 1
		pokemon_name = name_to_bytes(pokemon_names[str(pokemon_index)])
		set_value(battle_save, pokemon_name, memory.playerPartyNicks[i])
		copy_values(player_trainer_info, memory.wStringBuffer1, battle_save, memory.playerPartyOTs[i])

	copy_values(player_trainer_info, memory.wStringBuffer1, battle_save, memory.wPlayerName)
	set_value(battle_save, [memory.NAME_TERMINATOR], memory.playerNameEnd)

	if enemy_gender == "FEMALE" or (enemy_gender == "ENBY" and rng.random() > 0.5):
		set_value(battle_save, [0x1], memory.wPlayerGender)

	set_value(battle_save, [enemy_class], memory.wOtherTrainerClass)
	set_value(battle_save, [enemy_class], memory.wTrainerClass)
	set_value(battle_save, [enemy_index], memory.wOtherTrainerID)

	randomize_rdiv(battle_save, rng)

	# randomize textbox frame
	set_value(battle_save, [rng.randint(0, 8)], memory.wTextboxFrame)

	set_value(battle_save, [0x1], memory.wNumItems)
	item_id = memory.itemXAttack
	set_value(battle_save, [item_id, 0x1, 0xff], memory.wItems)

	return battle_save


def get_ai_action(battle_save: bytearray, base_save: str, working_save: str, out_save: str, trainer: Tuple[int, int],
                  rng: random.Random, current_player_items: bytearray = None) -> bytearray:
	ai_save = load_save(base_save)
	swap_pairings(battle_save, ai_save)

	set_value(ai_save, [trainer[0]], memory.wOtherTrainerClass)
	set_value(ai_save, [trainer[0]], memory.wTrainerClass)
	set_value(ai_save, [trainer[1]], memory.wOtherTrainerID)

	# Edge case -- the AI gets confused if the player mon has no HP
	if get_value(battle_save, memory.wEnemyMonHP) == b"\x00\x00":
		print("It's zero!")
		set_value(ai_save, [0x0, 0x1], memory.wBattleMonHP)

	randomize_rdiv(ai_save, rng)

	if current_player_items is not None:
		set_value(ai_save, current_player_items, memory.wEnemyTrainerItems)

	set_value(ai_save, get_enemy_used_moves(battle_save), memory.wPlayerUsedMoves)

	# TODO: we may need to update more values here. Check the disassembly.

	# These allow us to make the game think our Pokemon is always on the last turn of Perish Song
	# set_value(ai_save, wEnemySubStatus1[0], [0x10], 1)
	# set_value(ai_save, wEnemyPerishCount[0], [0x1], 1)

	write_file(working_save, ai_save)

	# Open the AI state, wait for the results
	call_bgb(in_save=working_save, out_save=out_save, breakpoint_list=[
		# for switching:
		'LoadEnemyMon',
		# for move selection:
		'PlayerTurn_EndOpponentProtectEndureDestinyBond', 'EnemyTurn_EndOpponentProtectEndureDestinyBond',
		'AI_Switch',
		'EnemyUsedFullHeal', 'EnemyUsedMaxPotion', 'EnemyUsedFullRestore', 'EnemyUsedPotion', 'EnemyUsedSuperPotion',
		'EnemyUsedHyperPotion', 'EnemyUsedXAccuracy', 'EnemyUsedGuardSpec', 'EnemyUsedDireHit', 'EnemyUsedXAttack',
		'EnemyUsedXDefend', 'EnemyUsedXSpeed', 'EnemyUsedXSpecial',
	], demo=files.AI_DEMO,
	         # hf=False, timeout=10000
	         )

	# Parse AI actions
	ai_output = load_save(out_save)
	return ai_output


def swap_pairings(source_save, target_save):
	# Copy data from battle save to ai save, swapping the player and enemy data

	for pairing in memory.player_enemy_pairs:
		player_address, enemy_address = pairing

		copy_values(source_save, player_address, target_save, enemy_address)
		copy_values(source_save, enemy_address, target_save, player_address)


def get_enemy_used_moves(input_save: bytearray) -> bytearray:
	# wPlayerUsed moves keeps track of the moves the enemy has seen the player use.
	# There's no equivalent for the enemy's moves, so this is a bit of a hack, looking for moves that aren't at full PP.
	# There are some edge cases where this fails, but they're relatively rare and this isn't used much anyway:
	# - If the enemy uses transform (they'll all be at less-than-full PP)
	# - If the enemy uses struggle (it should bump something off the list, but it won't)
	# - If the enemy switches out and back in (it should reset the list, but it won't reset PP)
	# - If the enemy uses a MysteryBerry (fortunately, no trainers do)
	# For completion's sake, some edge-cases that don't matter because enemies aren't players:
	# - If the enemy learns a move in the middle of battle (can happen for player Pokémon)
	# - If the enemy has used a PP Up
	# - If the enemy uses Sketch (no enemies have Smeargle)
	used_move_list = bytearray([0, 0, 0, 0])
	enemy_moves = get_value(input_save, memory.wEnemyMonMoves)
	enemy_pp = get_value(input_save, memory.wEnemyMonPP)
	for i, move in enumerate(enemy_moves):
		if move != 0:
			expected_pp = moves[move]["pp"]
			if enemy_pp[i] != expected_pp:
				used_move_list[used_move_list.index(0)] = move
				print(f"Enemy move {moves[move]['name']} used, expected PP {expected_pp} but got {enemy_pp[i]}")
	return used_move_list


def get_battle_mons(battle_save: bytearray) -> Tuple[Tuple[int, int, int, int], Tuple[int, int, int, int]]:
	return (
		(
			get_value(battle_save, memory.wBattleMonSpecies)[0],
			get_stat(get_value(battle_save, memory.wBattleMonHP)),
			get_stat(get_value(battle_save, memory.wBattleMonMaxHP)),
			get_value(battle_save, memory.wCurPartyMon)[0]
		),
		(
			get_value(battle_save, memory.wEnemyMonSpecies)[0],
			get_stat(get_value(battle_save, memory.wEnemyMonHP)),
			get_stat(get_value(battle_save, memory.wEnemyMonMaxHP)),
			get_value(battle_save, memory.wCurOTMon)[0]
		)
	)


def initial_testing():
	seed = str(random.randint(0, 1000000000))

	rng = random.Random(seed)

	# Set up working directory
	run_identifier = random.randint(1, 10000000)
	working_dir = os.path.abspath(f"W:/elo_world_scratch/crystal/{run_identifier}")
	output_dir = os.path.abspath(f"W:/elo_world_output/crystal/{run_identifier}")
	movie_working_dir = f"{working_dir}/movie"
	save_working_dir = f"{working_dir}/saves"
	demo_working_dir = f"{working_dir}/demo"
	movie_context = MovieContext(movie_name=str(run_identifier),
	                             movie_index=0,
	                             movie_working_dir=movie_working_dir,
	                             movie_output_dir=output_dir)

	for directory in [working_dir, output_dir, save_working_dir, demo_working_dir, movie_working_dir]:
		os.makedirs(directory, exist_ok=True)

	out_save_path = f"{save_working_dir}/{files.OUT_SAVE}"
	ai_input_save_path = f"{save_working_dir}/{files.AI_INPUT_SAVE}"
	ai_output_save_path = f"{save_working_dir}/{files.AI_OUTPUT_SAVE}"
	battle_save_path = f"{save_working_dir}/{files.BATTLE_SAVE}"
	out_demo_path = f"{demo_working_dir}/{files.OUT_DEMO}"

	shutil.copyfile(files.ROM_IMAGE, f"{save_working_dir}/{files.ROM_NAME}")
	shutil.copyfile(files.MEMORY_MAP, f"{save_working_dir}/{files.MEMORY_MAP_NAME}")
	shutil.copyfile(files.CHEAT_FILE, f"{save_working_dir}/{files.CHEAT_NAME}")

	# Randomly choose a player and enemy trainer

	player_trainer, enemy_trainer = rng.choice(raw_trainer_data), rng.choice(raw_trainer_data)

	player_class = player_trainer['class']
	player_index = player_trainer['instance']
	enemy_class = enemy_trainer['class']
	enemy_index = enemy_trainer['instance']
	#
	# player_class = 35
	# player_index = 1
	# enemy_class = 14
	# enemy_index = 1

	battle_log = start_new_battle(seed=seed,
	                              player=(player_class, player_index),
	                              enemy=(enemy_class, enemy_index))

	# Load the data for the player trainer
	base_save = load_save(files.BASE_SAVE)

	player_trainer_info = load_trainer_info(player_class, player_index, base_save, out_save_path)

	# Set up the initial battle state
	battle_save = set_up_battle_save(base_save, player_trainer_info, enemy_class, enemy_index, enemy_trainer["gender"],
	                                 rng)
	current_player_items = get_value(player_trainer_info, wEnemyTrainerItems)

	write_file(battle_save_path, battle_save)
	write_file(out_demo_path, generate_demo([]))

	while True:
		print(f"{current_player_items=}")
		# Play until we reach a menu or win/lose
		total_clocks = get_total_clocks(battle_save)
		breakpoint_condition = f"TOTALCLKS!=${total_clocks:x}"
		call_bgb(in_save=battle_save_path, out_save=battle_save_path, breakpoint_list=[
			f'BattleMenu/{breakpoint_condition}',
			f'SetUpBattlePartyMenu/{breakpoint_condition}',
			f'WinTrainerBattle/{breakpoint_condition}',
			f'LostBattle/{breakpoint_condition},',
		], demo=out_demo_path, movie_context=movie_context)

		battle_save = load_save(battle_save_path)

		player_battle_mon, enemy_battle_mon = get_battle_mons(battle_save)

		pc = get_program_counter(battle_save)
		print(f'Program counter: {pc:x}')

		# Which breakpoint did we hit?
		if pc == memory.breakpoints["WinTrainerBattle"]:
			# Player won!

			print("You win!")
			end_battle(battle_log, "PLAYER")
			break
		elif pc == memory.breakpoints["LostBattle"]:
			# Enemy won!

			print("You lose!")
			end_battle(battle_log, "ENEMY")
			break
		elif pc == memory.breakpoints["SetUpBattlePartyMenu"]:
			# AI is forced to switch out, what should we switch to?

			ai_output = get_ai_action(battle_save=battle_save,
			                          base_save=files.BASE_SWITCH_SAVE,
			                          working_save=ai_input_save_path,
			                          out_save=ai_output_save_path,
			                          trainer=(player_class, player_index),

			                          rng=rng)

			target_pokemon = get_value(ai_output, memory.wCurPartyMon)[0]
			current_pokemon_index = get_current_pokemon_index(battle_save)
			print("The selected pokemon was", target_pokemon, "and the current pokemon was",
			      current_pokemon_index)

			button_sequence = choose_pokemon(current_pokemon_index, target_pokemon)
			add_turn(battle_log, "FORCE_SWITCH", target_pokemon, player_battle_mon, enemy_battle_mon)
		else:
			# We're at the battle menu, what should we choose?

			ai_output = get_ai_action(battle_save=battle_save,
			                          base_save=files.BASE_AI_SAVE,
			                          working_save=ai_input_save_path,
			                          out_save=ai_output_save_path,
			                          trainer=(player_class, player_index),
			                          current_player_items=current_player_items,
			                          rng=rng)

			ai_pc = get_program_counter(ai_output)
			print(f'AI program counter: {ai_pc:x}')
			if ai_pc in memory.items.keys():
				print("The AI wants to use an item")

				set_value(battle_save, [0x1], memory.wNumItems)
				item_id = memory.items[ai_pc]
				set_value(battle_save, [item_id, 0x1, 0xff], memory.wItems)

				target_pokemon = get_value(battle_save, memory.wCurPartyMon)[0]
				current_pokemon_index = get_current_pokemon_index(battle_save)

				button_sequence = select_item(current_pokemon_index, target_pokemon)

				write_file(battle_save_path, battle_save)

				current_player_items[current_player_items.index(item_id)] = 0x0

				add_turn(battle_log, "ITEM", item_id, player_battle_mon, enemy_battle_mon)

			elif ai_pc == memory.breakpoints["AI_Switch"]:
				print("The AI wants to switcharino")
				target_pokemon = get_value(ai_output, memory.wEnemySwitchMonIndex)[0] - 1
				current_pokemon_index = get_current_pokemon_index(battle_save)
				print("The selected pokemon was", target_pokemon, "and the current pokemon was",
				      current_pokemon_index)

				button_sequence = select_switch() + choose_pokemon(current_pokemon_index, target_pokemon)

				add_turn(battle_log, "SWITCH", target_pokemon, player_battle_mon, enemy_battle_mon)

			else:
				selected_move_index = get_value(ai_output, memory.wCurEnemyMoveNum)[0]
				print("The selected move was", selected_move_index)
				current_move_index = get_value(battle_save, memory.wCurMoveNum)[0]

				button_sequence = select_move(current_move_index, selected_move_index)

				# TODO: how does struggle work?
				player_moves = get_value(battle_save, memory.wBattleMonMoves)

				add_turn(battle_log, "MOVE", player_moves[selected_move_index], player_battle_mon, enemy_battle_mon)

		write_file(out_demo_path, button_sequence)

	build_movie(movie_context)


if __name__ == '__main__':
	initial_testing()

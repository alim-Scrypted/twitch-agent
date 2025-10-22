import os, asyncio, random, time, websockets, json
from twitchio.ext import commands
from dotenv import load_dotenv
import requests

load_dotenv()

CHANNEL = os.getenv("TWITCH_CHANNEL")
BOT_NICK = os.getenv("TWITCH_BOT_USERNAME")
OAUTH = os.getenv("TWITCH_OAUTH_TOKEN")
BACKEND = os.getenv("BACKEND_BASE_URL", "http://127.0.0.1:8000")

async def post_async(*args, **kwargs):
    return await asyncio.to_thread(requests.post, *args, **kwargs)

class Bot(commands.Bot):
	def __init__(self):
		super().__init__(token=OAUTH, prefix="!", nick=BOT_NICK, initial_channels=[CHANNEL])
		self.current_poll = None
		self.poll_timer = None
		self.poll_votes = {}
		self.pending_prompts = []
		self.is_processing = False
		# Global, rate-limited chat queue and sender state
		self.chat_queue = asyncio.Queue()
		self.sender_task = None
		self.accepting_votes = False
		self.last_vote_announce = 0
		self.flush_event = asyncio.Event()
		self.scoreboard_task = None
	
	async def event_ready(self):
		print(f"Logged in as {self.nick}")
		print(f"Channel: {CHANNEL}")
		print(f"Connected channels: {[ch.name for ch in self.connected_channels]}")
		asyncio.create_task(self.main_processing_loop())
		self.sender_task = asyncio.create_task(self.chat_sender_loop())

	async def chat_sender_loop(self):
		"""Send all chat messages through a single, rate-limited queue with retries."""
		while True:
			msg = await self.chat_queue.get()
			# Handle flush marker without sending to chat
			if isinstance(msg, str) and msg == "__FLUSH__":
				try:
					self.flush_event.set()
				except Exception as e:
					print(f"⚠️ Flush event set error: {e}")
				# Safety delay to maintain spacing guarantees
				await asyncio.sleep(1.6)
				self.chat_queue.task_done()
				continue
			for attempt in range(3):
				try:
					channel = self.get_channel(CHANNEL)
					if channel:
						await channel.send(msg)
					else:
						print(f"❌ No channel {CHANNEL} (kept message): {msg[:60]}...")
					break
				except Exception as e:
					print(f"⚠️ Send error (attempt {attempt+1}): {e}")
					await asyncio.sleep(1.5 * (attempt + 1))
			# Safety delay to avoid rate limits
			await asyncio.sleep(1.6)
			self.chat_queue.task_done()

	async def queue_chat(self, message: str):
		await self.chat_queue.put(message)
	
	async def main_processing_loop(self):
		"""Main loop that handles 5-prompt selection and polling"""
		while True:
			try:
				# Keep only prompts that have a backend-assigned id
				self.pending_prompts = [p for p in self.pending_prompts if p and p.get("id")]
				print(f"🔍 Loop: {len(self.pending_prompts)} prompts, processing: {self.is_processing}")
				
				# Failsafe: reset if stuck
				if self.is_processing and self.poll_timer and self.poll_timer.done():
					print("⚠️ RESET: Poll timer done but still processing")
					self.is_processing = False
				
				if not self.is_processing and len(self.pending_prompts) >= 5:
					print("🚀 STARTING POLL!")
					selected_prompts = random.sample(self.pending_prompts, 5)
					await self.start_poll_session(selected_prompts)
				
				await asyncio.sleep(2)
			except Exception as e:
				print(f"❌ Loop error: {e}")
				self.is_processing = False
				await asyncio.sleep(5)
	
	async def start_poll_session(self, prompt_list):
		"""Start a 15-second poll session with 5 prompts"""
		print("🔄 Starting poll session...")
		self.is_processing = True
		self.current_poll = prompt_list
		self.poll_votes = {i: 0 for i in range(len(prompt_list))}
		self.accepting_votes = True
		# Reset flush event for this poll
		self.flush_event = asyncio.Event()
		# Ensure previous scoreboard is not running
		if self.scoreboard_task and not self.scoreboard_task.done():
			self.scoreboard_task.cancel()
			self.scoreboard_task = None

		channel = self.get_channel(CHANNEL)
		if not channel:
			print(f"❌ No channel {CHANNEL}")
			self.is_processing = False
			self.accepting_votes = False
			return

		# Queue poll header and options for reliable, rate-limited delivery
		await self.queue_chat("🎯 NEW POLL! Vote with !1, !2, !3, !4, or !5:")
		for i, prompt in enumerate(prompt_list):
			await self.queue_chat(f"{i+1}. {prompt['user']}: {prompt['text']}")
		await self.queue_chat("⏰ You have 15 seconds to vote!")
		# Insert flush marker and wait for it so timer starts after messages are sent
		await self.queue_chat("__FLUSH__")
		await self.flush_event.wait()
		print("✅ Poll messages flushed; starting timer and scoreboard")

		# Start timer and scoreboard after flush
		self.poll_timer = asyncio.create_task(self.poll_timer_func())
		self.scoreboard_task = asyncio.create_task(self.scoreboard_loop())
	
	async def poll_timer_func(self):
		"""15-second poll timer"""
		print("⏰ Timer started")
		await asyncio.sleep(15)
		print("⏰ Timer finished")
		await self.end_poll_session()
	
	async def end_poll_session(self):
		"""End poll and process the winning prompt"""
		print("🏁 Ending poll session")
		if not self.current_poll:
			print("❌ No current poll to end")
			self.is_processing = False
			self.accepting_votes = False
			return
		
		channel = self.get_channel(CHANNEL)
		if channel:
			# Winner with tiebreakers: votes desc, timestamp asc, then random
			counts = self.poll_votes
			if counts:
				max_votes = max(counts.values())
				tied = [i for i, c in counts.items() if c == max_votes]
				if len(tied) > 1:
					earliest_ts = min(self.current_poll[i].get("timestamp", float("inf")) for i in tied)
					tied_earliest = [i for i in tied if self.current_poll[i].get("timestamp", float("inf")) == earliest_ts]
					if len(tied_earliest) > 1:
						winner_index = random.choice(tied_earliest)
						await self.queue_chat("⚖️ Tie detected — breaking randomly among earliest submissions.")
					else:
						winner_index = tied_earliest[0]
						await self.queue_chat("⚖️ Tie detected — earliest submission wins.")
				else:
					winner_index = tied[0]
			else:
				winner_index = 0


			winner_prompt = self.current_poll[winner_index]
			vote_count = self.poll_votes[winner_index]
			
			# Announce results
			await self.queue_chat(f"🏆 WINNER: {winner_prompt['user']} with {vote_count} votes!")
			await self.queue_chat(f"📝 '{winner_prompt['text']}'")
			await self.queue_chat("✅ This wish is my command!")

			# Notify backend: mark winner and enqueue cleaned prompt for AI bridge
			try:
				wid = winner_prompt.get("id")
				if wid:
					r = await post_async(f"{BACKEND}/prompt/win", json={"id": wid}, timeout=5)
					if r.ok:
						print(f"🧠 Winner #{wid} queued for AI bridge")
					else:
						print(f"⚠️ Failed to queue winner: {r.text}")
			except Exception as e:
				print(f"⚠️ Failed to mark winner: {e}")

		# Stop scoreboard if running
		if self.scoreboard_task and not self.scoreboard_task.done():
			self.scoreboard_task.cancel()
			self.scoreboard_task = None
		
		# Move processed prompts to history
		try:
			prompt_ids = [p.get("id") for p in self.current_poll if p.get("id")]
			if prompt_ids:
				r = await post_async(f"{BACKEND}/move-to-history", json=prompt_ids, timeout=5)
				if r.ok:
					print(f"📚 Moved {len(prompt_ids)} prompts to history")
				else:
					print(f"⚠️ Failed to move prompts to history: {r.text}")
		except Exception as e:
			print(f"⚠️ Failed to move prompts to history: {e}")
		
		# Clean up - remove ALL prompts from current poll from pending queue
		print(f"🧹 Removing {len(self.current_poll)} prompts from queue")
		for prompt in self.current_poll:
			if prompt in self.pending_prompts:
				self.pending_prompts.remove(prompt)
				print(f"   Removed: {prompt['user']}: {prompt['text'][:30]}...")
		
		print(f"📊 Queue now has {len(self.pending_prompts)} prompts remaining")
		
		# Reset poll state
		self.current_poll = None
		self.poll_votes = {}
		self.is_processing = False
		self.accepting_votes = False
		print("✅ Poll session ended - ready for next poll")
	
	@commands.command(name="prompt")
	async def submit_prompt(self, ctx: commands.Context):
		"""Submit a new prompt to the queue"""
		idea = ctx.message.content.partition(" ")[2].strip()
		if not idea:
			return await self.queue_chat("Usage: !prompt <idea>")
		
		try:
			# Send to backend
			r = await post_async(f"{BACKEND}/submit", 
				json={"user": ctx.author.name, "type": "prompt", "text": idea}, 
				timeout=5)
			
			# Backend returns 200 with either {id} or {error}
			try:
				resp = r.json()
			except Exception:
				resp = {}
			sid = (resp or {}).get("id")
			if sid:
				prompt_data = {
					"user": ctx.author.name,
					"text": idea,
					"timestamp": time.time(),
					"id": sid
				}
				self.pending_prompts.append(prompt_data)
				print(f"📝 Added prompt #{sid}: {idea[:50]}... (Queue: {len(self.pending_prompts)})")
				await self.queue_chat(f"📝 Queued! ({len(self.pending_prompts)} total)")
			else:
				err = (resp or {}).get("error") or r.text
				await self.queue_chat(f"❌ Rejected: {err or 'Failed to submit'}")
		except Exception as e:
			await self.queue_chat(f"❌ Error: {e.__class__.__name__}")
	
	@commands.command(name="1")
	async def vote_1(self, ctx: commands.Context):
		await self.cast_vote(ctx, 0)
	
	@commands.command(name="2")
	async def vote_2(self, ctx: commands.Context):
		await self.cast_vote(ctx, 1)
	
	@commands.command(name="3")
	async def vote_3(self, ctx: commands.Context):
		await self.cast_vote(ctx, 2)
	
	@commands.command(name="4")
	async def vote_4(self, ctx: commands.Context):
		await self.cast_vote(ctx, 3)
	
	@commands.command(name="5")
	async def vote_5(self, ctx: commands.Context):
		await self.cast_vote(ctx, 4)
	
	async def cast_vote(self, ctx: commands.Context, prompt_index: int):
		"""Cast a vote for a specific prompt in the current poll"""
		if not self.current_poll or prompt_index not in self.poll_votes or not self.accepting_votes:
			return

		# Increment integer counter to allow repeat votes accumulation
		self.poll_votes[prompt_index] += 1
		
		vote_counts = [self.poll_votes[i] for i in range(len(self.current_poll))]
		print(f"🗳️ {ctx.author.name} voted for option {prompt_index + 1}. Current votes: {vote_counts}")
		# No per-vote chat message to avoid rate limit interference

	async def scoreboard_loop(self):
		"""Periodically announce current vote counts during an active poll."""
		try:
			while self.accepting_votes and self.current_poll:
				await asyncio.sleep(10)
				if not self.accepting_votes or not self.current_poll:
					break
				counts = [self.poll_votes.get(i, 0) for i in range(len(self.current_poll))]
				parts = [f"{i+1}:{counts[i]}" for i in range(len(counts))]
				await self.queue_chat("📈 Votes: " + " | ".join(parts))
		except asyncio.CancelledError:
			pass
	
	@commands.command(name="forcepoll")
	async def force_poll(self, ctx: commands.Context):
		"""Force start a poll"""
		if self.is_processing:
			return await self.queue_chat("⏳ Already processing")
		
		if len(self.pending_prompts) < 2:
			return await self.queue_chat(f"📝 Need 2+ prompts, have {len(self.pending_prompts)}")
		
		num_prompts = min(5, len(self.pending_prompts))
		selected_prompts = random.sample(self.pending_prompts, num_prompts)
		await self.start_poll_session(selected_prompts)
		await self.queue_chat(f"🔄 Force started poll with {len(selected_prompts)} prompts!")
	
	@commands.command(name="status")
	async def status(self, ctx: commands.Context):
		"""Show bot status"""
		status_msg = f"📊 Status: {len(self.pending_prompts)} prompts, processing: {self.is_processing}"
		if self.current_poll:
			status_msg += f", active poll: {len(self.current_poll)} options"
		await self.queue_chat(status_msg)
	
	@commands.command(name="queue")
	async def show_queue(self, ctx: commands.Context):
		"""Show current prompt queue"""
		if not self.pending_prompts:
			return await self.queue_chat("📭 No prompts in queue")
		
		queue_info = f"📋 Queue ({len(self.pending_prompts)} prompts): "
		for i, prompt in enumerate(self.pending_prompts[:5]):  # Show first 5
			queue_info += f"{i+1}. {prompt['user']}: {prompt['text'][:25]}... "
		
		await self.queue_chat(queue_info)

if __name__ == "__main__":
	Bot().run()
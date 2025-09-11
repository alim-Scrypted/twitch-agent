import os, asyncio, random, time, websockets, json
from twitchio.ext import commands
from dotenv import load_dotenv
import requests

load_dotenv()

CHANNEL = os.getenv("TWITCH_CHANNEL")
BOT_NICK = os.getenv("TWITCH_BOT_USERNAME")
OAUTH = os.getenv("TWITCH_OAUTH_TOKEN")
BACKEND = os.getenv("BACKEND_BASE_URL", "http://127.0.0.1:8000")

class Bot(commands.Bot):
	def __init__(self):
		super().__init__(token=OAUTH, prefix="!", nick=BOT_NICK, initial_channels=[CHANNEL])
		self.current_poll = None
		self.poll_timer = None
		self.poll_votes = {}
		self.pending_prompts = []
		self.is_processing = False
	
	async def event_ready(self):
		print(f"Logged in as {self.nick}")
		print(f"Channel: {CHANNEL}")
		print(f"Connected channels: {[ch.name for ch in self.connected_channels]}")
		asyncio.create_task(self.main_processing_loop())
	
	async def main_processing_loop(self):
		"""Main loop that handles 5-prompt selection and polling"""
		while True:
			try:
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

		channel = self.get_channel(CHANNEL)
		if not channel:
			print(f"❌ No channel {CHANNEL}")
			self.is_processing = False
			return

		# Send poll header (resilient)
		try:
			await channel.send("🎯 NEW POLL! Vote with !1, !2, !3, !4, or !5:")
		except Exception as e:
			print(f"❌ Send header error: {e}")
		await asyncio.sleep(0.8)
		
		# Send each prompt as separate message for better formatting; continue on individual errors
		for i, prompt in enumerate(prompt_list):
			try:
				await channel.send(f"{i+1}. {prompt['user']}: {prompt['text']}")
			except Exception as e:
				print(f"❌ Send prompt {i+1} error: {e}")
			await asyncio.sleep(0.8)
		
		try:
			await channel.send("⏰ You have 15 seconds to vote!")
		except Exception as e:
			print(f"❌ Send timer notice error: {e}")
		print("✅ Poll messages attempted")

		# Start timer regardless of send errors
		self.poll_timer = asyncio.create_task(self.poll_timer_func())
	
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
			return
		
		channel = self.get_channel(CHANNEL)
		if channel:
			# Find winner using integer counts
			winner_index = max(self.poll_votes.keys(), key=lambda i: self.poll_votes[i])
			winner_prompt = self.current_poll[winner_index]
			vote_count = self.poll_votes[winner_index]
			
			# Announce results
			await channel.send(f"🏆 WINNER: {winner_prompt['user']} with {vote_count} votes!")
			await channel.send(f"📝 '{winner_prompt['text']}'")
			await channel.send("✅ This wish is my command...")
		
		# Move processed prompts to history
		try:
			prompt_ids = [p.get("id") for p in self.current_poll if p.get("id")]
			if prompt_ids:
				r = requests.post(f"{BACKEND}/move-to-history", json=prompt_ids, timeout=5)
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
		print("✅ Poll session ended - ready for next poll")
	
	@commands.command(name="prompt")
	async def submit_prompt(self, ctx: commands.Context):
		"""Submit a new prompt to the queue"""
		idea = ctx.message.content.partition(" ")[2].strip()
		if not idea:
			return await ctx.send("Usage: !prompt <idea>")
		
		try:
			# Send to backend
			r = requests.post(f"{BACKEND}/submit", 
				json={"user": ctx.author.name, "type": "prompt", "text": idea}, 
				timeout=5)
			
			if r.ok:
				sid = r.json().get("id")
				prompt_data = {
					"user": ctx.author.name,
					"text": idea,
					"timestamp": time.time(),
					"id": sid
				}
				self.pending_prompts.append(prompt_data)
				print(f"📝 Added prompt #{sid}: {idea[:50]}... (Queue: {len(self.pending_prompts)})")
				await ctx.send(f"📝 Queued! ({len(self.pending_prompts)} total)")
			else:
				await ctx.send("❌ Failed to submit")
		except Exception as e:
			await ctx.send(f"❌ Error: {e.__class__.__name__}")
	
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
		if not self.current_poll or prompt_index not in self.poll_votes:
			return

		# Increment integer counter to allow repeat votes accumulation
		self.poll_votes[prompt_index] += 1
		
		vote_counts = [self.poll_votes[i] for i in range(len(self.current_poll))]
		print(f"🗳️ {ctx.author.name} voted for option {prompt_index + 1}. Current votes: {vote_counts}")
		await ctx.send(f"🗳️ {ctx.author.name} voted for option {prompt_index + 1}! Current votes: {vote_counts}")
	
	@commands.command(name="forcepoll")
	async def force_poll(self, ctx: commands.Context):
		"""Force start a poll"""
		if self.is_processing:
			return await ctx.send("⏳ Already processing")
		
		if len(self.pending_prompts) < 2:
			return await ctx.send(f"📝 Need 2+ prompts, have {len(self.pending_prompts)}")
		
		num_prompts = min(5, len(self.pending_prompts))
		selected_prompts = random.sample(self.pending_prompts, num_prompts)
		await self.start_poll_session(selected_prompts)
		await ctx.send(f"🔄 Force started poll with {len(selected_prompts)} prompts!")
	
	@commands.command(name="status")
	async def status(self, ctx: commands.Context):
		"""Show bot status"""
		status_msg = f"📊 Status: {len(self.pending_prompts)} prompts, processing: {self.is_processing}"
		if self.current_poll:
			status_msg += f", active poll: {len(self.current_poll)} options"
		await ctx.send(status_msg)
	
	@commands.command(name="queue")
	async def show_queue(self, ctx: commands.Context):
		"""Show current prompt queue"""
		if not self.pending_prompts:
			return await ctx.send("📭 No prompts in queue")
		
		queue_info = f"📋 Queue ({len(self.pending_prompts)} prompts): "
		for i, prompt in enumerate(self.pending_prompts[:5]):  # Show first 5
			queue_info += f"{i+1}. {prompt['user']}: {prompt['text'][:25]}... "
		
		await ctx.send(queue_info)

if __name__ == "__main__":
	Bot().run()

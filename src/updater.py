import classes as c  # our classes
from helpers import *  # our helpers
import config  # config
import discord  # main discord libary
from discord.ext import commands  # import commands extension
from discord.ext import tasks
import json
import traceback
import time
from datetime import datetime
import dbl
import os
import io
import traceback
import socket
import menus as m
import concurrent.futures._base as base
import asyncio
import aiohttp


class UpdateResult(c.JSON):
    def __init__(self, result: bool, serverObj: c.ARKServer, playersObj: c.PlayersList, id: int, reason: c.ARKServerError = None):
        self.result = result
        self.serverObj = serverObj
        self.playersObj = playersObj
        self.id = id
        self.ip = self.serverObj.ip
        self.reason = reason

    def successful(self):
        return self.result


class NeoUpdater(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.cfg = config.Config()
        # count of concurrent functions to run
        self.workersCount = self.cfg.workersCount
        self.handlers = []  # array of handler classes which would handle results
        self.additions = []  # array of additional classes which would update some info
        self.servers = None # local cache of all servers from DB. Updated on every iteration
        self.serversIds = None # list of ids of the servers from local cache for faster searching

    def cog_unload(self):  # on unload
        self.updaterLoop.cancel()  # cancel the task
                                # self.destroy will run anything to destroy 

    # generates array of ids of servers from local cache
    async def flattenCache(self):
        # list comprehension is faster
        return [i[0] for i in self.servers] 

    # searches for an id in local cache (self.servers)
    async def searchCache(self, id: int):
        try:
            # search for that id in flattened locla cache 
            position = self.serversIds.index(id)
        # if not found
        except ValueError:
            # return none
            return None
        # return server record from found position
        return self.servers[position]
        
    # performs SQL request using aiomysql pool instead of regular function
    async def makeAsyncRequest(self, SQL, params=()):
        conn = await self.sqlPool.acquire()  # acquire one connecton from the pool
        async with conn.cursor() as cur:  # with cursor as cur
            await cur.execute(SQL, params)  # execute SQL with parameters
            result = await cur.fetchall()  # fetch all results
            await conn.commit()  # commit changes
        self.sqlPool.release(conn)  # release current connection to the pool
        return result  # return result

    # let's do anything normal __init__ can't do 
    async def init(self): 
        self.httpSession = aiohttp.ClientSession()  # for use in http API's
        self.sqlPool = await aiomysql.create_pool(host=self.cfg.dbHost, port=3306,  # somehow works see:
                                                  # https://github.com/aio-libs/aiomysql/issues/574
                                                  user=self.cfg.dbUser, password=self.cfg.dbPass,
                                                  db=self.cfg.DB, loop=asyncio.get_running_loop(), minsize=self.workersCount)
    
    # function that updates some server
    async def updateServer(self, Ip: str, Id: int):
        server = c.ARKServer(Ip) # construct classes 
        players = c.PlayersList(Ip)
        updateServer = server.AGetInfo() # make coroutines
        updatePlayers = players.AgetPlayersList()
        
        try:
            # run coroutines concurrently
            results = await asyncio.gather(updateServer, updatePlayers)
        except c.ARKServerError as e: # if fails
            return UpdateResult(False, None, None, Id, e) # return fail and reason
        
        return UpdateResult(True, results[0], results[1], Id) # else return success

    async def save(self,results):
        tasks = [] # list of tasks to run concurrently
        # for each server on list
        for result in results:
            # if update is successful
            if (result.successful()):
                # make request
                task = self.makeAsyncRequest("UPDATE servers SET LastOnline=1, OfflineTrys=0, ServerObj=%s, PlayersObj=%s WHERE Id=%s",
                result.serverObj.toJSON(), result.playersObj.toJSON(), result.id)
            else:
                # find the server in cache
                cachedServer = await self.searchCache(result.id)
                # if server is found
                if (cachedServer != None):
                    # make request
                    task = self.makeAsyncRequest("UPDATE servers SET LastOnline=0, OfflineTrys=%s WHERE Id=%s",
                                                cachedServer[7] + 1, cachedServer[0])
                # if server is not found
                else:
                    # make request (won't track how many times it was offline when we checked)
                    task = self.makeAsyncRequest("UPDATE servers SET LastOnline=0, OfflineTrys=1 WHERE Id=%s",
                                                cachedServer[0])
        # after each task generated
        # run them concurrently
        await asyncio.gather(*tasks)

    # main updater loop
    @tasks.loop(seconds=100.0)
    async def update(self):
        print("Entered updater loop!")
        self.servers = await self.makeAsyncRequest("SELECT * FROM servers") # update local cache
        self.serversIds = await self.flattenCache() # make array with ids only     
        serversCount = self.servers.__len__() # get how many servers we need to update

        tasks = [] # list of tasks to run concurrently
        # for each server
        for i in range(1,serversCount):
            # make coroutine
            task = self.updateServer(i)
            # append new task to task list
            tasks[:0] = task 
            # if enough tasks generated 
            if (tasks.__len__() >= self.workersCount):
                # run them concurrently
                results = await asyncio.gather(*tasks)
                # empty the list of tasks
                tasks = []
                #########
                # Space for more functions
                #########
                # save results in DB
                await self.save(results)
        # if there is some tasks left 
        if (tasks.__len__() != 0):
            # run them concurrently
            results = await asyncio.gather(*tasks)
            # empty the list of tasks
            tasks = []
            # save results in DB
            await self.save(results)

    # will be executed before main loop starts
    @update.before_loop
    async def before_update(self):
        await self.init()
        print("Inited updater loop!")

    # on error handler
    @update.error
    async def onError(self,error):
        errors = traceback.format_exception(
        type(error), error, error.__traceback__)
        errors_str = ''.join(errors)
        await sendToMe(errors_str,True)

    # will be executed before main loop will be destroyed
    @update.after_loop
    async def destroy(self):
        self.sqlPool.close()
        await self.httpSession.close()
        print("Destroyed updater loop!")

class Updater(commands.Cog):
    '''
    Updates record for server in DB
    '''

    def __init__(self, bot):
        self.bot = bot
        self.cfg = config.Config()
        # count of concurrent functions to run
        self.workersCount = self.cfg.workersCount
        self.server_list = []  # list of objects (UpdateResults)
        self.fetchedUrls = 0
        print('Init')
        self.printer.start()
        self.t = c.Translation()

    async def initPool(self):
        '''
        init a pool of sql connections to DB
        '''
        #print('Started initing pool')
        #cfg = config.Config()
        # self.pool = await aiomysql.create_pool(host=cfg.dbHost, port=3306,
        #                              user=cfg.dbUser, password=cfg.dbPass,
        #                              db=cfg.DB, loop=asyncio.get_running_loop(), minsize=self.workersCount)
        print('Done initing pool (stub)!')

    async def makeAsyncRequest(self, SQL, params=()):
        return await makeAsyncRequest(SQL, params)

    async def makeAsyncRequestOld(self, SQL, params=()):
        '''
        Async method to make SQL requests using connections from pool inited in initPool()
        '''
        conn = await self.pool.acquire()  # acquire one connecton from the pool
        async with conn.cursor() as cur:  # with cursor as cur
            await cur.execute(SQL, params)  # execute SQL with parameters
            result = await cur.fetchall()  # fetch all results
            await conn.commit()  # commit changes
        self.pool.release(conn)  # release current connection to the pool
        return result  # return result

    def cog_unload(self):  # on unload
        self.printer.cancel()  # cancel the task
        self.pool.terminate()  # terminate pool of connections

    async def server_notificator(self, server):
        '''
        Function to notify about server events like server went up or down 
        '''
        #print('entered message sender')
        channels = self.notificationsList  # all notification records
        channels = list(filter(lambda x: str(server[0]) in [i.strip() for i in x[4][1:-1].split(
            ',')], self.notificationsList))  # find any channels that must receive notifications ????
        if (channels.__len__() <= 0):  # if we have no channels to send notifications to
            return  # return
        print(f'Found a notification record for {server[0]} server!')
        if (server[1] == 1):  # if server went online
            ARKServer = server[2]  # get server object
            for channel in channels:  # for each channel we got from DB
                discordChannel = self.bot.get_channel(
                    channel[1])  # get that channel
                if (discordChannel == None):  # if channel not found
                    # debug it
                    print(
                        f'Channel not found for server : {server[0]} Channel id :{channel[1]}')
                    continue  # and continue
                else:  # if channel is found
                    # get an alias for that server
                    aliases = await getAlias(0, discordChannel.guild.id, ARKServer.ip)
                    if (aliases == ''):  # if no alias exist
                        # name is striped name
                        name = await stripVersion(ARKServer)
                    else:
                        name = aliases  # else name is server's alias
                    # send notification
                    await discordChannel.send(f'Server {name} ({ARKServer.map}) went online!')
                    print(
                        f'sent message for went online for server {server[0]}')
        if (server[1] == 2):  # if server went offline
            ARKServer = server[2]  # get server object (taken from DB)
            for channel in channels:  # for each channel we need to send notification to
                # if we already sent notification (I wander how this would happen?)
                if (channel[3] == 1):
                    continue
                discordChannel = self.bot.get_channel(
                    channel[1])  # get channel stored in DB
                if (discordChannel == None):  # if channel is not found
                    # debug it
                    print(
                        f'Channel not found for server : {server[0]} Channel id :{channel[1]}')
                    continue  # and continue
                else:
                    aliases = await getAlias(0, discordChannel.guild.id, ARKServer.ip)
                    if (aliases == ''):
                        name = ARKServer.name.find(f'- ({ARKServer.version})')
                        name = ARKServer.name[:name].strip()
                    else:
                        name = aliases
                    await discordChannel.send(f'Server {name} ({ARKServer.map}) went offline!')
                    print(
                        f'sent message for went offline for server {server[0]}')
                    await makeAsyncRequest('UPDATE notifications SET Sent=1 WHERE Id=%s', (channel[0],))

        # if (server[1] == 2 ): # server went offline
        #    db_server = makeRequest('SELECT OfflineTrys FROM servers WHERE Id=%s',(server[0],))
        #    channels = makeRequest('SELECT * FROM notifications WHERE ServersIds LIKE %s AND Type=3',(f'%{server[0]}%',))
        #    if (channels.__len__() >= 1):
        #        ARKServer = server[2]
        #        for channel in channels:
        #            discordChannel = self.bot.get_channel(channel[1])
        #            if (discordChannel == None):
        #                print(f'Channel not found! Channel id :{channel[1]}')
        #            else:
        #                await discordChannel.send(f'Server {ARKServer.name} ({ARKServer.map}) ({ARKServer.ip}) went offline!')
        #                print('sent message for went offline')

    async def notificator(self, serverList):
        #print('Entered notificator!')
        for server in serverList:
            await self.server_notificator(server)
            #print(f'Sent server notifications for server : {server[0]}!')
            # player_notificator()

    async def update_server(self, serverId):  # universal server upgrader
        # select from local cache (self.servers)
        server = list(filter(lambda x: x[0] == serverId, self.servers))
        server = server[0]  # select first result
        ip = server[1]  # get server's ip
        result = []  # where we store the result of the function
        try:  # standart online/offline check
            # get info about server
            serverObj = await c.ARKServer(ip).AGetInfo()
            # get players list
            playersList = await c.PlayersList(ip).AgetPlayersList()
            # get server object from DB
            oldObj = c.ARKServer.fromJSON(server[4])
            if (not hasattr(oldObj, 'battleURL')):  # if we don't have battle url already in the DB object
                #print(f'We don`t have battle URl for server {ip} {getattr(c.ARKServer.fromJSON(server[4]),"battleURL","nothing")}')
                # get it
                battleURL = await self.battleAPI.getBattlemetricsUrl(serverObj)
                self.fetchedUrls += 1  # increase debug counter
                if (battleURL):  # if we fetched the url
                    # serverObj.battleURL = battleURL # put it in
                    # push it into the object
                    setattr(serverObj, 'battleURL', battleURL)
            else:
                # magic code probably to fix an old bug
                setattr(serverObj, 'battleURL', oldObj.battleURL)
            # update DB record
            await makeAsyncRequest('UPDATE servers SET ServerObj=%s , PlayersObj=%s , LastOnline=1 , OfflineTrys=0 WHERE Ip =%s', (serverObj.toJSON(), playersList.toJSON(), ip))
            # if previously server was offline (check LastOnline column)
            if (bool(server[6]) == False):
                # return server went online (return status 1 and two new objects)
                result = [1, serverObj, playersList, 0]
            else:
                # return unchanged (return status 3 and two new objects)
                result = [3, serverObj, playersList, 0]
        except c.ARKServerError:  # catch my own error
            # update DB (add one to OfflineTrys and set LastOnline to 0)
            await makeAsyncRequest('UPDATE servers SET LastOnline=0,OfflineTrys=%s WHERE Ip=%s', (server[7]+1, ip,))
            if (bool(server[6]) == True):  # if server was online
                result = [2, c.ARKServer.fromJSON(server[4]), c.PlayersList.fromJSON(
                    server[5]), server[7]+1]  # return server went offline
            else:
                result = [3, c.ARKServer.fromJSON(server[4]), c.PlayersList.fromJSON(
                    server[5]), server[7]+1]  # return unchanged
        except BaseException as e:
            await sendToMe(e, self.bot)
            result = [1, c.ARKServer('1.1.1.1:1234'),
                      c.PlayersList('1.1.1.1:1234')]
        # change in notifications : send them as soon as possible so updates would be faster
        await self.server_notificator(result)  # send notifications
        return result

    @tasks.loop(seconds=110.0)
    async def printer(self):  # entrypoint
        self.fetchedUrls = 0  # it was resetting the variable in the wrong place !
        await sendToMe('Entered updater!', self.bot)  # debug
        start = time.perf_counter()  # start timer
        chunksTime = []  # list to hold times it takes to process each chunk
        # fetch all notifications records
        self.notificationsList = await makeAsyncRequest('SELECT * FROM notifications WHERE Type=3')
        # fetch all servers (it must be heavy ?)
        self.servers = await makeAsyncRequest('SELECT * FROM servers')
        serverCount = self.servers.__len__()  # get current count of servers
        try:
            print('Entered updater!')  # debug
            servers = self.servers
            server_list = []  # empty list
            # from 1 to server count with step of number of workers
            for i in range(1, serverCount - self.workersCount, self.workersCount):
                localStart = time.perf_counter()
                # print(f'Updating servers: {[server[0] for server in  servers[i:i+self.workersCount]]}') # debug
                # generate tasks to complete (update servers)
                tasks = [self.update_server(i[0])
                         for i in servers[i:i+self.workersCount]]
                # run all generated tasks in paralel
                results = await asyncio.gather(*tasks)
                a = 0  # in stead of traditional i lol
                for result in results:  # loop throuh results
                    # append to server list it id,and result from update function (status, two object and offlinetrys)
                    server_list.append(
                        [servers[i+a][0], result[0], result[1], result[2]])
                    a += 1
                localEnd = time.perf_counter()
                chunksTime.append(localEnd - localStart)
            # if (self.bot.is_ready()): # if bot's cache is ready
            #    print('handling notifications') # handle notifictaions
            #    updater_end = time.perf_counter()
            #    await self.notificator(server_list) # pass the list with servers and their statuses to the function
            #    end = time.perf_counter() # end performance timer
            #    await sendToMe(f'It took {updater_end - start:.4f} seconds to update all servers!\n{end - updater_end:.4f} sec. to send all notifications.\n{end - start:.4f} sec. in total',self.bot) # debug
            # else: # if not
            end = time.perf_counter()  # end performance timer
            # debug
            await sendToMe(f"It took {end - start:.4f} seconds to update all servers!\nNotifications weren`t sent because bot isn't ready\n{end - start:.4f} sec. in total", self.bot)
            if (len(chunksTime) <= 0):
                await sendToMe(f'Max chunk time is: {0:.4f}\nMin chunk time: {0:.4f}\nAverage time is:{0:.4f}\nChunk lenth is: {self.workersCount}\nUpdate queue lenth is: {self.servers.__len__()}\nWARNING: `chunksTime.__len__()` <= 0! chunksTime: `{chunksTime}`', self.bot)
            else:
                await sendToMe(f'Max chunk time is: {max(chunksTime):.4f}\nMin chunk time: {min(chunksTime):.4f}\nAverage time is:{sum(chunksTime)/len(chunksTime):.4f}\nChunk lenth is: {self.workersCount}\nUpdate queue lenth is: {self.servers.__len__()}', self.bot)
            await sendToMe(f'Fetched {self.fetchedUrls} urls!', self.bot)
        except KeyError as error:
            await self.on_error(error)
            # await deleteServer(server[1])
        except BaseException as error:  # if any exception
            await self.on_error(error)

    # @printer.error
    async def on_error(self, error):
        errors = traceback.format_exception(
            type(error), error, error.__traceback__)
        time = int(time.time())
        date = datetime.utcfromtimestamp(time).strftime('%Y-%m-%d %H:%M:%S')
        errors_str = ''.join(errors)
        message = f'Error in updater loop!\n It happend at `{date}`\n```{errors_str}```'
        if (errors_str >= 2000):
            try:
                await sendToMe(message[:1975] + '`\nEnd of first part', self.bot)
                await sendToMe(message[1975:-1], self.bot)
            except BaseException as e:
                await sendToMe('Lenth of error message is over 4k!', self.bot)
                await sendToMe(e, self.bot)

    @printer.before_loop
    async def before_printer(self):
        print('waiting...')
        # await self.bot.wait_until_ready() why waiste all this time when we can update DB while cache is updating?
        self.session = aiohttp.ClientSession()  # get aiohttps's session
        self.battleAPI = c.BattleMetricsAPI(
            self.session)  # construct API class
        await self.initPool()
        print('done waiting')

    @commands.bot_has_permissions(add_reactions=True, read_messages=True, send_messages=True, manage_messages=True, external_emojis=True)
    @commands.command()
    async def watch(self, ctx):
        selector = m.Selector(ctx, self.bot, self.t)
        server = await selector.select()
        if server == '':
            return
        Type = 3
        ip = server.ip
        serverId = await makeAsyncRequest('SELECT Id FROM servers WHERE Ip=%s', (ip,))
        serverId = serverId[0][0]
        notifications2 = await makeAsyncRequest('SELECT * FROM notifications WHERE DiscordChannelId=%s', (ctx.channel.id,))
        notifications = await makeAsyncRequest('SELECT * FROM notifications WHERE DiscordChannelId=%s AND Type=%s', (ctx.channel.id, Type,))
        if (notifications.__len__() <= 0):
            ids = []
            ids.append(serverId)
            await makeAsyncRequest('INSERT INTO `notifications`(`DiscordChannelId`, `ServersIds`, `Data`, `Sent`, `Type`) VALUES (%s,%s,"{}",0,%s)', (ctx.channel.id, json.dumps(ids), Type,))
            await ctx.send(self.t.l['done'])
            return
        else:
            ids = json.loads(notifications[0][4])
            if serverId in ids:
                await ctx.send('You already receive notifications about this server!')
                return
            else:
                ids.append(serverId)
                await makeAsyncRequest('UPDATE notifications SET ServersIds=%s WHERE DiscordChannelId=%s AND Type=%s', (json.dumps(ids), ctx.channel.id, Type,))
                await ctx.send(self.t.l['done'])
                return

    @commands.bot_has_permissions(add_reactions=True, read_messages=True, send_messages=True, manage_messages=True, external_emojis=True)
    @commands.command()
    async def unwatch(self, ctx):
        selector = m.Selector(ctx, self.bot, self.t)
        server = await selector.select()
        if server == '':
            return
        ip = server.ip
        serverId = await makeAsyncRequest('SELECT Id FROM servers WHERE Ip=%s', (ip,))
        serverId = serverId[0][0]
        notifications = await makeAsyncRequest('SELECT * FROM notifications WHERE DiscordChannelId=%s AND Type=3 AND ServersIds LIKE %s', (ctx.channel.id, f'%{serverId}%',))
        if (notifications.__len__() <= 0):
            await ctx.send("You don't have any notifications for this server!")
            return
        else:
            newServerlist = json.loads(notifications[0][4])
            newServerlist.remove(serverId)
            newServerlist = json.dumps(newServerlist)
            await makeAsyncRequest('UPDATE notifications SET ServersIds=%s WHERE Id=%s', (newServerlist, notifications[0][0]))
            await ctx.send('Done !')


def setup(bot):
    bot.add_cog(Updater(bot))

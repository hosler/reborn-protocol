#include <catch2/catch_all.hpp>

#include <any>
#include <atomic>
#include <cstdlib>
#include <fstream>
#include <iomanip>
#include <memory>
#include <optional>
#include <sstream>
#include <string>
#include <string_view>
#include <unordered_map>
#include <variant>
#include <vector>

#include <CSocket.h>

#include <BabyDI.h>
#include <Server.h>
#include <npcserver/NPCServer.h>
#include <object/NPC.h>
#include <object/Player.h>
#include <player/PlayerClient.h>
#include <player/PlayerRC.h>
#include <scripting/ScriptContainers.h>
#include <scripting/ScriptTypes.h>
#include <scripting/gs1/GS1Variables.h>
#include <scripting/gs1/GS1Visitor.h>
#include <scripting/gs1/ScriptEngineGS1.h>
#include <utilities/Log.h>

using namespace preagonal;
using namespace std::string_view_literals;

std::atomic_bool shutdownProgram{false};

namespace
{
struct OracleCase
{
	std::string id;
	std::string kind;
	std::vector<std::string> events{"created"};
	std::string body;
};

std::string jsonString(std::string_view value)
{
	std::ostringstream out;
	out << '"';
	for (unsigned char c : value)
	{
		switch (c)
		{
			case '"': out << "\\\""; break;
			case '\\': out << "\\\\"; break;
			default:
				if (c < 0x20 || c >= 0x7f)
					out << "\\u" << std::hex << std::setw(4) << std::setfill('0') << static_cast<unsigned>(c) << std::dec;
				else
					out << static_cast<char>(c);
		}
	}
	out << '"';
	return out.str();
}

std::string trim(std::string value)
{
	auto first = value.find_first_not_of(" \t\r\n");
	if (first == std::string::npos)
		return {};
	auto last = value.find_last_not_of(" \t\r\n");
	return value.substr(first, last - first + 1);
}

std::vector<OracleCase> readCases(std::istream& input)
{
	std::vector<OracleCase> cases;
	OracleCase* current = nullptr;
	bool sawEvents = false;
	std::string line;
	while (std::getline(input, line))
	{
		if (line.starts_with("=====CASE "))
		{
			cases.push_back({.id = trim(line.substr(10)), .kind = "script"});
			current = &cases.back();
			sawEvents = false;
		}
		else if (line.starts_with("=====MATH "))
		{
			cases.push_back({.id = trim(line.substr(10)), .kind = "math", .events = {}});
			current = &cases.back();
			sawEvents = false;
		}
		else if (line.starts_with("=====STR "))
		{
			cases.push_back({.id = trim(line.substr(9)), .kind = "str", .events = {}});
			current = &cases.back();
			sawEvents = false;
		}
		else if (line.starts_with("=====EVENTS ") && current && !sawEvents && current->body.empty())
		{
			current->events.clear();
			std::stringstream names(line.substr(12));
			std::string name;
			while (std::getline(names, name, ','))
				if (!(name = trim(name)).empty()) current->events.push_back(name);
			sawEvents = true;
		}
		else if (current)
		{
			current->body += line;
			current->body += '\n';
		}
	}
	return cases;
}

std::optional<ScriptEventType> eventType(std::string_view name)
{
	static const std::unordered_map<std::string_view, ScriptEventType> events{
		{"created", ScriptEventType::CREATED}, {"timeout", ScriptEventType::TIMEOUT},
		{"playerchats", ScriptEventType::PLAYERCHATS}, {"playerenters", ScriptEventType::PLAYERENTERS},
		{"playerleaves", ScriptEventType::PLAYERLEAVES}, {"playertouchsme", ScriptEventType::PLAYERTOUCHSME},
		{"playertouchsother", ScriptEventType::PLAYERTOUCHSOTHER}, {"playerhurt", ScriptEventType::PLAYERHURT},
		{"playerdies", ScriptEventType::PLAYERDIES}, {"playerlaysitem", ScriptEventType::PLAYERLAYSITEM},
		{"playerlogin", ScriptEventType::PLAYERLOGIN}, {"playerlogout", ScriptEventType::PLAYERLOGOUT},
		{"washit", ScriptEventType::WASHIT}, {"wasshot", ScriptEventType::WASSHOT},
		{"waspelt", ScriptEventType::WASPELT}, {"wasthrown", ScriptEventType::WASTHROWN},
		{"exploded", ScriptEventType::EXPLODED}, {"compusdied", ScriptEventType::COMPUSDIED},
		{"movementfinished", ScriptEventType::MOVEMENTFINISHED}, {"npcwarped", ScriptEventType::NPCWARPED},
		{"privatemessage", ScriptEventType::PRIVATEMESSAGE}, {"serverlistconnect", ScriptEventType::SERVERLISTCONNECT},
		{"serverside", ScriptEventType::CUSTOM},
	};
	auto found = events.find(name);
	return found == events.end() ? std::nullopt : std::optional{found->second};
}

std::string dumpValue(const GameVariable& variable)
{
	if (!variable.getters.empty())
		return R"({"live":true})";

	std::ostringstream out;
	out << '{';
	bool comma = false;
	auto field = [&](std::string_view name) {
		if (comma) out << ',';
		comma = true;
		out << '"' << name << "\":";
	};
	if (auto value = variable.value.get<double>()) { field("num"); out << std::setprecision(17) << value->get(); }
	if (auto value = variable.value.get<std::string>()) { field("text"); out << jsonString(value->get()); }
	if (auto value = variable.value.get<std::vector<double>>())
	{
		field("array"); out << '[';
		for (size_t i = 0; i < value->get().size(); ++i) { if (i) out << ','; out << std::setprecision(17) << value->get()[i]; }
		out << ']';
	}
	if (auto value = variable.value.get<bool>()) { field("bool"); out << (value->get() ? "true" : "false"); }
	out << '}';
	return out.str();
}

std::string dumpStore(const GameVariableStore* store)
{
	if (!store) return "{}";
	std::ostringstream out;
	out << '{';
	bool comma = false;
	for (const auto& [name, variable] : store->store)
	{
		if (comma) out << ',';
		comma = true;
		out << jsonString(name) << ':' << dumpValue(*variable);
	}
	out << '}';
	return out.str();
}

void clearSnapshots(GameVariableStore& store)
{
	for (auto it = store.store.begin(); it != store.store.end();)
	{
		if (it->second->getters.empty())
			it = store.store.erase(it);
		else
			++it;
	}
}

struct ServerFixture
{
	ServerFixture()
	{
		log::networkdump.disabled = log::npc.disabled = log::rc.disabled = true;
		log::script.disabled = log::server.disabled = true;
		BabyDI_RELEASE(Server);
		server = BabyDI_PROVIDE(Server, new Server("test"));
		server->getSettings().set("serverside", true);
		server->loadNPCServer();
		auto player = std::dynamic_pointer_cast<Player>(server->getNPCServer()->getPlayerNPCServer());
		gs1::setPlayerVariables(player->account.variables, player);
		auto npc = server->getNPCServer()->addNPC("door.png"sv, ""sv, nullptr, TilePosition{20.0f, 30.0f}, NPCTYPE_OBJECT);
		npc->name = "Test";
		testNPC = npc->id;
		auto client = std::make_shared<PlayerClient>(new CSocket(), server->getPlayerIdGenerator().getAvailableId());
		auto rc = std::make_shared<PlayerRC>(new CSocket(), server->getPlayerIdGenerator().getAvailableId());
		server->addPlayer(client, client->getId()); server->addPlayer(rc, rc->getId());
		server->getNPCServer()->playerLogin(client); server->getNPCServer()->playerLogin(rc);
	}
	NPCID testNPC = NPCID_GEN_DATABASE_LOCALN;
	Server* server = nullptr;
	gs1::ScriptEngineGS1 engine;
};
}

TEST_CASE_METHOD(ServerFixture, "gs1 oracle batch", "[oracle]")
{
	const char* inputPath = std::getenv("GS1_ORACLE_IN");
	const char* outputPath = std::getenv("GS1_ORACLE_OUT");
	REQUIRE(inputPath != nullptr);
	REQUIRE(outputPath != nullptr);
	std::ifstream input(inputPath);
	std::ofstream output(outputPath);
	REQUIRE(input.good());
	REQUIRE(output.good());

	for (const auto& item : readCases(input))
	{
		clearSnapshots(server->getNPC(testNPC)->scripting.variables);
		clearSnapshots(server->getNPCServer()->getPlayerNPCServer()->account.variables);
		clearSnapshots(server->Scripting.variables);
		output << "{\"id\":" << jsonString(item.id) << ",\"kind\":" << jsonString(item.kind);
		if (item.kind == "math")
		{
			auto result = engine.processMathExpression(trim(item.body), source::FromNPC(testNPC));
			REQUIRE(result.has_value());
			output << ",\"compile_error\":null,\"result\":" << std::setprecision(17) << *result << "}\n";
			continue;
		}
		if (item.kind == "str")
		{
			auto result = engine.processStringExpression(trim(item.body), source::FromNPC(testNPC));
			REQUIRE(result.has_value());
			output << ",\"compile_error\":null,\"result\":" << jsonString(*result) << "}\n";
			continue;
		}

		auto result = engine.compileScript(item.id, item.body);
		if (std::holds_alternative<std::string>(result))
		{
			output << ",\"compile_error\":" << jsonString(std::get<std::string>(result)) << "}\n";
			continue;
		}
		auto& context = std::get<ScriptExecutionContext>(result);
		auto wrapper = std::any_cast<gs1::GS1ScriptWrapper>(context.script.get());
		if (!wrapper)
		{
			output << R"(,"compile_error":"internal: null wrapper"})" << '\n';
			continue;
		}
		wrapper->variables.defaultLifetime = variables::Lifetime::NORMAL;
		auto contextPtr = std::shared_ptr<ScriptExecutionContext>(&context, [](ScriptExecutionContext*) {});
		output << ",\"compile_error\":null,\"executed\":[";
		for (size_t i = 0; i < item.events.size(); ++i)
		{
			auto type = eventType(item.events[i]);
			INFO("unknown-event:" << item.events[i]);
			REQUIRE(type.has_value());
			ScriptEvent event{.type = *type, .initiator = source::FromPlayer(NPCServerPlayerID)};
			REQUIRE(engine.execute(event, source::FromNPC(testNPC), contextPtr));
			if (i) output << ',';
			output << jsonString(item.events[i]);
		}
		output << "],\"stores\":{\"builtIn\":" << dumpStore(wrapper->visitor->builtInStore)
		       << ",\"npc\":" << dumpStore(&server->getNPC(testNPC)->scripting.variables)
		       << ",\"player\":" << dumpStore(&server->getNPCServer()->getPlayerNPCServer()->account.variables)
		       << ",\"server\":" << dumpStore(&server->Scripting.variables) << "}}\n";
	}
	REQUIRE(output.good());
	SUCCEED();
}

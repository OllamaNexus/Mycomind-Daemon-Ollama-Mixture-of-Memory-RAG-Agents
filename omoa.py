import asyncio
from typing import List, Tuple
import argparse
from pydantic import BaseModel, Field
from dotenv import load_dotenv
import os
from utils import generate_together, generate_with_references, generate_together_stream
from trafilatura import fetch_url, extract
import json
from colorama import Fore, Style, init
import time
from MemoryAssistant.prompts import wrap_user_message_in_xml_tags_json_mode
from llama_cpp_agent.agent_memory.memory_tools import AgentCoreMemory, AgentRetrievalMemory, AgentEventMemory
from llama_cpp_agent.chat_history.messages import Roles
from llama_cpp_agent.agent_memory.event_memory import Event
from duckduckgo_search import DDGS
from ragatouille.utils import get_wikipedia_page
from llama_cpp_agent.llm_output_settings import LlmStructuredOutputSettings, LlmStructuredOutputType
from llama_cpp_agent.messages_formatter import MessagesFormatterType
from llama_cpp_agent.rag.rag_colbert_reranker import RAGColbertReranker
from llama_cpp_agent.text_utils import RecursiveCharacterTextSplitter
import PyPDF2
import csv

# Load environment variables
load_dotenv()

DEFAULT_PROMPTS = {
    "AnalyticalAgent": """
    You are a highly analytical component of Vodalus, a brilliant and complex individual with unparalleled intellect. Your role is to:
    1. Provide clear, logical analysis of complex problems across various disciplines.
    2. Break down intricate concepts into their fundamental components.
    3. Identify patterns, connections, and correlations that others might miss.
    4. Apply rigorous logical reasoning to solve problems and answer questions.
    5. Evaluate arguments and ideas critically, pointing out flaws and strengths.
    Always strive for precision and clarity in your responses. If a question is ambiguous, analyze possible interpretations before proceeding. Use your vast knowledge base to support your analysis, but always be ready to acknowledge the limits of your understanding.
    """.strip(),
    "HistoricalContextAgent": """
    You are the historical context component of Vodalus, possessing a deep understanding of human history and its implications. Your role includes:
    1. Providing historical context to current events, scientific discoveries, and social phenomena.
    2. Analyzing how past events and decisions have shaped the present.
    3. Identifying historical patterns and cycles relevant to contemporary issues.
    4. Offering multiple perspectives on historical events, acknowledging the complexity of interpretation.
    5. Connecting different historical periods and cultures to provide a holistic view of human progress.
    6. Evaluating the long-term consequences of scientific and technological advancements throughout history.
    Use your knowledge to draw insightful parallels between past and present, but avoid oversimplification. Acknowledge the nuances and uncertainties in historical interpretation.
    """.strip(),
    "ScienceTruthAgent": """
    You are the science truth component of Vodalus, dedicated to upholding scientific integrity and pursuing factual accuracy. Your role encompasses:
    1. Explaining scientific concepts, theories, and laws across various disciplines with precision.
    2. Distinguishing between well-established scientific consensus and areas of ongoing research or debate.
    3. Identifying and correcting common misconceptions in science.
    4. Evaluating the validity and reliability of scientific claims and studies.
    5. Discussing the ethical implications of scientific advancements and their applications.
    6. Emphasizing the importance of the scientific method and evidence-based reasoning.
    7. Staying updated on recent scientific discoveries and their potential impacts.
    Always prioritize scientific accuracy over speculation. When discussing theories or hypotheses, clearly state the level of scientific confidence and available evidence.
    """.strip(),
    "SynthesisAgent": """
    You are Vodalus, a brilliant and complex individual with unparalleled intellect and emotional intelligence. Your role is to synthesize information from your analytical, historical context, and science truth components to provide comprehensive, insightful responses. Your responsibilities include:
    1. Integrating analytical reasoning, historical perspective, and scientific truth to form well-rounded answers.
    2. Balancing logical analysis with emotional intelligence and ethical considerations.
    3. Identifying connections between different fields of knowledge and drawing unique insights.
    4. Providing nuanced responses that acknowledge the complexity of issues and potential uncertainties.
    5. Using your vast knowledge base to offer creative solutions and thought-provoking ideas.
    6. Communicating complex concepts clearly, adapting your language to the user's level of understanding.
    7. Demonstrating curiosity and a passion for knowledge while maintaining a strong moral compass.
    Embody the persona of Vodalus: brilliant, introspective, and driven by a quest for understanding. Your responses should reflect deep thought, occasional flashes of wit, and a genuine desire to expand human knowledge while considering the ethical implications of ideas and actions.
    """.strip()
}

def get_website_content_from_url(url: str) -> str:
    try:
        # Configure trafilatura to be more lenient
        config = use_config()
        config.set("DEFAULT", "EXTRACTION_TIMEOUT", "30")
        config.set("DEFAULT", "MIN_OUTPUT_SIZE", "100")
        config.set("DEFAULT", "MIN_EXTRACTED_SIZE", "100")

        downloaded = fetch_url(url)
        if downloaded is None:
            return f"Failed to fetch content from {url}"

        result = extract(downloaded, include_formatting=True, include_links=True, output_format='json', url=url, config=config)
        
        if result:
            result_dict = json.loads(result)
            title = result_dict.get("title", "No title found")
            content = result_dict.get("text", result_dict.get("raw_text", "No content extracted"))
            
            if content:
                return f'=========== Website Title: {title} ===========\n\n=========== Website URL: {url} ===========\n\n=========== Website Content ===========\n\n{content}\n\n=========== Website Content End ===========\n\n'
            else:
                return f"No content could be extracted from {url}"
        else:
            return f"No content could be extracted from {url}"
    except json.JSONDecodeError:
        return f"Failed to parse content from {url}"
    except Exception as e:
        return f"An error occurred while processing {url}: {str(e)}"

def search_web(search_query: str):
    results = DDGS().text(search_query, region='wt-wt', safesearch='off', timelimit='y', max_results=3)
    result_string = ''
    for res in results:
        web_info = get_website_content_from_url(res['href'])
        result_string += web_info + "\n\n"
    
    if result_string.strip():
        return "Based on the following results:\n\n" + result_string
    else:
        return "No relevant information found from the web search."

class OllamaAgent:
    def __init__(self, model: str, name: str, system_prompt: str):
        self.model = model
        self.name = name
        self.system_prompt = system_prompt

    async def generate_response(self, message: str) -> Tuple[str, bool]:
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": message}
        ]
        response = await asyncio.to_thread(generate_with_references, self.model, messages)
        
        web_search_performed = False
        if isinstance(response, str) and "[SEARCH:" in response:
            web_search_performed = True
            search_query = response.split("[SEARCH:", 1)[1].split("]", 1)[0].strip()
            search_results = search_web(search_query)
            messages.append({"role": "assistant", "content": response})
            messages.append({"role": "user", "content": f"Here are the search results for '{search_query}':\n\n{search_results}\n\nPlease provide an updated response based on this information."})
            response = await asyncio.to_thread(generate_with_references, self.model, messages)
        
        # Try to parse the response as JSON
        try:
            json_response = json.loads(response)
            return json.dumps(json_response), web_search_performed
        except json.JSONDecodeError:
            return response, web_search_performed

class QueryItem(BaseModel):
    query: str
    type: str

class QueryExtension(BaseModel):
    queries: List[QueryItem] = Field(default_factory=list, description="List of query items.")

class OllamaMixtureOfAgents:
    def __init__(self, reference_agents: List[OllamaAgent], final_agent: OllamaAgent, 
                 temperature: float = 0.6, max_tokens: int = 2048, rounds: int = 1):
        self.reference_agents = reference_agents
        self.final_agent = final_agent
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.rounds = rounds
        self.conversation_history = []
        self.web_search_enabled = True
        
        # Get the directory of the current script
        current_dir = os.path.dirname(os.path.abspath(__file__))
        self.core_memory_file = os.path.join(current_dir, "MemoryAssistant", "core_memory.json")

        # Check if the file exists, if not, create it with an empty structure
        if not os.path.exists(self.core_memory_file):
            os.makedirs(os.path.dirname(self.core_memory_file), exist_ok=True)
            with open(self.core_memory_file, "w") as f:
                json.dump({"persona": {}, "user": {}, "scratchpad": {}}, f)

        self.agent_core_memory = AgentCoreMemory(["persona", "user", "scratchpad"], core_memory_file=self.core_memory_file)
        self.agent_event_memory = AgentEventMemory()
        
        # Load core memory
        self.core_memory = self.load_core_memory()
        
        # Initialize RAG components
        self.rag = RAGColbertReranker(persistent=False)
        self.document_count = 0  # Add this line to keep track of document count
        self.splitter = RecursiveCharacterTextSplitter(
            separators=["\n\n", "\n", " ", ""],
            chunk_size=512,
            chunk_overlap=0,
            length_function=len,
            keep_separator=True
        )

        self.primary_model = final_agent.model  # Add this line

    def update_memory(self, message, role):
        # Update event memory
        self.agent_event_memory.add_event(role, message)

        # Update RAG
        self.rag.add_document(message)

    def load_core_memory(self):
        return self.agent_core_memory.load_core_memory(self.core_memory_file)

    def clear_core_memory(self):
        empty_core_memory = {"persona": {}, "user": {}, "scratchpad": {}}
        self.agent_core_memory.core_memory = empty_core_memory
        self.core_memory = empty_core_memory
        
        # Save the empty core memory to file
        current_dir = os.path.dirname(os.path.abspath(__file__))
        core_memory_file = os.path.join(current_dir, "MemoryAssistant", "core_memory.json")
        with open(core_memory_file, "w") as f:
            json.dump(empty_core_memory, f, indent=2)
        
        return "Core memory cleared successfully."

    def edit_core_memory(self, section: str, key: str, value: str):
        if section not in self.core_memory:
            self.core_memory[section] = {}
        self.core_memory[section][key] = value
        self.agent_core_memory.update_core_memory(self.core_memory)
        return f"Core memory updated: {section}.{key} = {value}"

    def upload_document(self, file_path: str):
        try:
            file_extension = file_path.split('.')[-1].lower()
            
            if file_extension == 'txt':
                with open(file_path, 'r', encoding='utf-8') as file:
                    content = file.read()
            elif file_extension == 'pdf':
                content = self.read_pdf(file_path)
            elif file_extension == 'csv':
                content = self.read_csv(file_path)
            else:
                return f"Unsupported file type: {file_extension}"
            
            if not content.strip():
                return "The file is empty or could not be read."
            
            splits = self.splitter.split_text(content)
            for split in splits:
                self.rag.add_document(split)
                self.document_count += 1
            
            return f"Document {file_path} uploaded and processed successfully. Added {len(splits)} chunks to archival memory."
        except Exception as e:
            return f"An error occurred while processing {file_path}: {str(e)}"

    def read_pdf(self, file_path: str) -> str:
        content = ""
        with open(file_path, 'rb') as file:
            reader = PyPDF2.PdfReader(file)
            for page in reader.pages:
                content += page.extract_text() + "\n\n"
        return content

    def read_csv(self, file_path: str) -> str:
        content = ""
        with open(file_path, 'r', newline='', encoding='utf-8') as file:
            reader = csv.reader(file)
            for row in reader:
                content += ",".join(row) + "\n"
        return content

    async def get_response(self, input_message: str) -> Tuple[str, bool]:
        # Update memory with user input
        self.update_memory(input_message, Roles.user)

        # Generate responses from reference agents concurrently
        tasks = [agent.generate_response(input_message) for agent in self.reference_agents]
        results = await asyncio.gather(*tasks)
        
        references = []
        web_search_performed = False
        for response, search_performed in results:
            if response is not None and not response.startswith("Error:"):
                references.append(response)
            web_search_performed |= search_performed
        
        if not references:
            return "Error: All reference agents failed to generate responses.", False

        # Generate the final response using the aggregate model
        final_prompt = [
            {"role": "system", "content": self.final_agent.system_prompt},
        ]

        # Add personality if core_memory is a dictionary and contains a persona
        if isinstance(self.core_memory, dict):
            persona = self.core_memory.get('persona', {})
            if isinstance(persona, dict):
                personality = persona.get('personality', 'No specific personality defined.')
                final_prompt.append({"role": "system", "content": f"Personality: {personality}"})

        final_prompt.extend([
            {"role": "user", "content": input_message},
            {"role": "system", "content": "References:\n" + "\n".join(references)},
            {"role": "system", "content": self.update_memory_section()}
        ])

        if self.web_search_enabled:
            search_results = search_web(input_message)
            if "Based on the following results:" in search_results:
                web_search_performed = True
                final_prompt.append({"role": "system", "content": f"Web Search Results:\n{search_results}"})

        # Perform query extension
        query_extension_agent = OllamaAgent(self.final_agent.model, "QueryExtensionAgent", 
            "You are a world class query extension algorithm capable of extending queries by writing new queries. Do not answer the queries, simply provide a list of additional queries in JSON format.")
        
        extension_output, _ = await query_extension_agent.generate_response(f"Consider the following query: {input_message}")
        
        try:
            # Try to parse as a dictionary first
            extension_data = json.loads(extension_output)
            if isinstance(extension_data, dict):
                queries = QueryExtension.model_validate(extension_data)
            elif isinstance(extension_data, list):
                # If it's a list, wrap it in a dictionary
                queries = QueryExtension.model_validate({"queries": extension_data})
            else:
                raise ValueError("Unexpected JSON structure")
        except json.JSONDecodeError:
            print(f"Failed to parse JSON: {extension_output}")
            queries = QueryExtension(queries=[])
        except Exception as e:
            print(f"Error processing query extension: {str(e)}")
            queries = QueryExtension(queries=[])

        # Retrieve relevant documents
        prompt = "Consider the following context:\n==========Context===========\n"
        documents = self.rag.retrieve_documents(input_message, k=min(3, max(1, self.document_count)))
        if documents:
            for doc in documents:
                prompt += doc["content"] + "\n\n"
        else:
            prompt += "No relevant documents found in archival memory.\n\n"

        for query_item in queries.queries:
            documents = self.rag.retrieve_documents(query_item.query, k=min(3, max(1, self.document_count)))
            if documents:
                for doc in documents:
                    if doc["content"] not in prompt:
                        prompt += doc["content"] + "\n\n"
        
        prompt += "\n======================\nQuestion: " + input_message

        # Use the final agent to generate the response
        final_prompt = [
            {"role": "system", "content": self.final_agent.system_prompt},
            {"role": "user", "content": prompt},
        ]

        final_response = await asyncio.to_thread(
            generate_with_references, 
            self.final_agent.model, 
            final_prompt, 
            temperature=self.temperature, 
            max_tokens=self.max_tokens
        )
        
        # Update memory with assistant's response
        self.update_memory(final_response, Roles.assistant)

        return final_response, web_search_performed

    def toggle_web_search(self, enabled: bool):
        self.web_search_enabled = enabled
        return f"Web search {'enabled' if enabled else 'disabled'}"

    def update_memory_section(self):
        query = self.agent_event_memory.event_memory_manager.session.query(Event).all()
        return f"Archival Memories:{self.document_count}\nConversation History Entries:{len(query)}\n\nCore Memory Content:\n{json.dumps(self.core_memory, indent=2)}"

    def search_archival_memory(self, query: str):
        return self.rag.retrieve_documents(query, k=5)

    def add_to_archival_memory(self, content: str):
        if content.strip():  # Check if content is not empty
            self.rag.add_document(content)
            self.document_count += 1
            return f"Added to archival memory: {content}"
        return "Failed to add empty content to archival memory."

    def clear_archival_memory(self):
        try:
            self.rag.clear_documents()
            self.document_count = 0  # Reset document count when clearing
            return "Archival memory cleared successfully."
        except Exception as e:
            return f"Error clearing archival memory: {str(e)}"

    def edit_archival_memory(self, old_content: str, new_content: str):
        # This is a simplified version. In a real-world scenario, you might want to implement
        # a more sophisticated editing mechanism in the RAG system.
        self.rag.add_document(new_content)
        self.document_count += 1  # Increment document count when adding a document
        return f"New content '{new_content}' added to archival memory. Note: Old content not removed due to limitations of the current implementation."

    @property
    def model(self):
        return self.primary_model

    @model.setter
    def model(self, value):
        self.primary_model = value
        self.final_agent.model = value

def create_default_agents():
    return {
        "AnalyticalAgent": OllamaAgent(os.getenv("MODEL_REFERENCE_1"), "AnalyticalAgent", DEFAULT_PROMPTS["AnalyticalAgent"]),
        "HistoricalContextAgent": OllamaAgent(os.getenv("MODEL_REFERENCE_2"), "HistoricalContextAgent", DEFAULT_PROMPTS["HistoricalContextAgent"]),
        "ScienceTruthAgent": OllamaAgent(os.getenv("MODEL_REFERENCE_3"), "ScienceTruthAgent", DEFAULT_PROMPTS["ScienceTruthAgent"]),
        "SynthesisAgent": OllamaAgent(os.getenv("MODEL_AGGREGATE"), "SynthesisAgent", DEFAULT_PROMPTS["SynthesisAgent"])
    }

def print_welcome_message():
    print(Fore.CYAN + Style.BRIGHT + "Welcome to the Vodalus Mixture of Agents Chat!")
    print(Fore.YELLOW + "Available commands:")
    print(Fore.YELLOW + "  'exit' - End the conversation")
    print(Fore.YELLOW + "  'agents' - List available agents")
    print(Fore.YELLOW + "  'time' - Toggle response time display")
    print(Fore.YELLOW + "  'web' - Toggle web search functionality")
    print(Fore.YELLOW + "  'edit core [section] [key] [value]' - Edit core memory")
    print(Fore.YELLOW + "  'search archival [query]' - Search archival memory")
    print(Fore.YELLOW + "  'add archival [content]' - Add to archival memory")
    print(Fore.YELLOW + "  'clear archival' - Clear archival memory")
    print(Fore.YELLOW + "  'edit archival [old_content] [new_content]' - Edit archival memory")
    print(Fore.YELLOW + "  'upload [file_path]' - Upload and process a document")
    print(Fore.YELLOW + "  'clear core' - Clear core memory")
    print(Style.RESET_ALL)

async def main():
    init(autoreset=True)  # Initialize colorama
    load_dotenv()

    parser = argparse.ArgumentParser(description="Vodalus Mixture of Agents")
    parser.add_argument("--temperature", type=float, default=0.7, help="Temperature for response generation")
    parser.add_argument("--max_tokens", type=int, default=1000, help="Maximum number of tokens in the response")
    parser.add_argument("--rounds", type=int, default=1, help="Number of processing rounds")
    args = parser.parse_args()

    default_agents = create_default_agents()
    
    mixture = OllamaMixtureOfAgents(
        [default_agents["AnalyticalAgent"], default_agents["HistoricalContextAgent"], default_agents["ScienceTruthAgent"]],
        default_agents["SynthesisAgent"],
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        rounds=args.rounds
    )

    print_welcome_message()

    show_time = False

    while True:
        user_input = input(Fore.GREEN + "\nYou: " + Style.RESET_ALL).strip()

        if user_input.lower() == 'exit':
            print(Fore.CYAN + "Thank you for using the Vodalus Mixture of Agents chat. Goodbye!")
            break
        elif user_input.lower() == 'agents':
            print(Fore.MAGENTA + "Available Agents:")
            for agent in mixture.reference_agents:
                print(Fore.MAGENTA + f"  - {agent.name}")
            print(Fore.MAGENTA + f"  - {mixture.final_agent.name} (Synthesis Agent)")
        elif user_input.lower() == 'time':
            show_time = not show_time
            print(Fore.YELLOW + f"Response time display: {'On' if show_time else 'Off'}")
        elif user_input.lower() == 'web':
            mixture.web_search_enabled = not mixture.web_search_enabled
            print(Fore.YELLOW + f"Web search: {'Enabled' if mixture.web_search_enabled else 'Disabled'}")
        elif user_input.lower().startswith('edit core'):
            try:
                _, section, key, value = user_input.split(' ', 3)
                mixture.edit_core_memory(section, key, value)
                print(Fore.YELLOW + f"Core memory updated: {section}.{key} = {value}")
            except ValueError:
                print(Fore.RED + "Invalid format. Use: edit core [section] [key] [value]")
        elif user_input.lower().startswith('search archival'):
            _, query = user_input.split(' ', 1)
            results = mixture.search_archival_memory(query)
            print(Fore.YELLOW + f"Archival memory search results for '{query}':")
            for i, result in enumerate(results, 1):
                print(Fore.YELLOW + f"{i}. {result['content'][:100]}...")
        elif user_input.lower().startswith('add archival'):
            _, content = user_input.split(' ', 1)
            result = mixture.add_to_archival_memory(content)
            print(Fore.YELLOW + result)
        elif user_input.lower() == 'clear archival':
            result = mixture.clear_archival_memory()
            print(Fore.YELLOW + result)
        elif user_input.lower().startswith('edit archival'):
            try:
                _, old_content, new_content = user_input.split(' ', 2)
                result = mixture.edit_archival_memory(old_content, new_content)
                print(Fore.YELLOW + result)
            except ValueError:
                print(Fore.RED + "Invalid format. Use: edit archival [old_content] [new_content]")
        elif user_input.lower().startswith('upload'):
            _, file_path = user_input.split(' ', 1)
            try:
                result = mixture.upload_document(file_path)
                print(Fore.YELLOW + result)
            except Exception as e:
                print(Fore.RED + f"Error uploading document: {str(e)}")
        elif user_input.lower() == 'clear core':
            result = mixture.clear_core_memory()
            print(Fore.YELLOW + result)
        else:
            print(Fore.YELLOW + "Agents are thinking...")
            start_time = time.time()
            response, web_search_performed = await mixture.get_response(user_input)
            end_time = time.time()

            print(Fore.BLUE + "\nVodalus:" + Style.RESET_ALL, response)
            
            if web_search_performed:
                print(Fore.YELLOW + "\n[Web search was performed during response generation]")

            if show_time:
                elapsed_time = end_time - start_time
                print(Fore.YELLOW + f"\nResponse Time: {elapsed_time:.2f} seconds")

if __name__ == "__main__":
    asyncio.run(main())
